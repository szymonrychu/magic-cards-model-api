"""HTTP endpoints the browser UI uses to boot and manage a lobby-booted game.

Mounted under `/api/lobby` (2b's T8, map's namespace split): every route in
this module is lobby-owned, and moved off the api-owned `/api` prefix so the
URL space itself already matches where each side of the eventual six-way cut
lives - the same reasoning `api_decks.py`'s docstring gives for the opposite
move (T5).

`serve.py` only boots games from `--deck`/`--match` command-line arguments,
read once at process start. A UI needs to boot a game *at request time*, from
a bundled `decks/*.txt` file or from text a human pasted or uploaded - so
`POST /api/lobby/games` calls the exact same engine path
(`decks.load_deck_into_game`, CR 103's whole start-of-game sequence) that
`serve.py`'s `_boot_from_spec` already uses, just from an HTTP body instead
of argv.

Deck resolution and legality themselves - `_resolve_all`,
`validate_decklist_text`, `GET /api/decks*`, `POST /api/decks/validate` -
moved to `api_decks.py` (2b's T5, map Finding 1): they need the engine and
`scryfall.py`, dependencies the lobby side of the eventual cut must not
carry. This module reaches that endpoint through `api_client.ApiClient`
rather than importing its function - the seam the cut later swaps for
`httpx` - so a decklist is still validated before either game is registered,
just not by importing the api component's internals to do it.

This module never touches game rules itself - every actual rule (construction
limits, CR 103's start-of-game sequence) is enforced by the engine through
`decks.py`. It only adapts an HTTP body to those existing calls.
"""

from __future__ import annotations

import os
from typing import Any, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import ai_pregame, ai_seat, audit, game_access, lobby, oidc
from .agent_settings import AgentSettings
from .api_client import ApiClient, InProcessApiClient
from .decks import load_deck_into_game
from .repository import RECENT_RECORDS_WINDOW, Advisor, GameUnloadable
from .schemas import RequestModel, Username
from .store import known_players, new_seat_uids, player_for_advisor_uid, store

router = APIRouter(prefix="/api/lobby", tags=["lobby"], route_class=game_access.GameAccessRoute)

#: The lobby's one dependency on the api component, for now an in-process
#: call (`api_client.py`'s own docstring). Module-level so a test can swap it
#: the way `store.py`'s singleton is swapped, without every call site taking
#: a client argument it has no other use for.
api_client: ApiClient = InProcessApiClient()


def max_live_games() -> int:
    """How many lobby-booted games may be live at once, `MAX_LIVE_GAMES` in
    the environment (default 4). Enforced by `start_game_from_decklists`
    against `store.live_ui_games()`; a finished game (`Game.is_over`, CR
    104.1) does not count against it, only End game frees up a live one."""
    return int(os.environ.get("MAX_LIVE_GAMES", "4") or 4)


class NewUiPlayer(RequestModel):
    username: Username
    decklist_text: str = Field(min_length=1)
    ai: bool = False
    """Whether this seat is meant to be driven by the AI-seat runner - a UI
    hint only (`ai_seat.py` is a separate, opt-in call); carried here so the
    response can tell the UI which uid to auto-start without a second lookup."""
    buddy: bool = False
    """Whether this seat gets a **buddy** - an advisor (`repository.Advisor`)
    watching this player's own view, proposing plays and never making one
    (`game_access.py` refuses every mutating request an advisor uid makes).

    Creation-time only, by design: a buddy is minted here, with its own uid,
    and from then on it is restarted and stopped through the same
    `ai-seat/start|stop` routes a seat uses. There is no mid-game toggle.

    **Only with `ai: false`.** A buddy advises a human; an AI seat acts
    through its own seat uid and never reads the advice ledger, so the pair
    is refused rather than minting an advisor nothing would read."""


class NewUiGame(RequestModel):
    players: list[NewUiPlayer] = Field(min_length=2, max_length=2)
    seed: int | None = None


class NewUiGameResponse(BaseModel):
    game_id: str
    seat_uids: dict[str, str]
    admin_key: str | None = None
    ai_players: list[str]
    agents: dict[str, AgentSettings]
    """Every AI seat's model, effort and thinking budget - always
    `AgentSettings.default` (2026-09-19: model/effort/thinking budget are a
    fixed server-side deployment choice, not a per-request one any more, so
    this is never anything else)."""
    advisor_uids: dict[str, str] = {}
    """player name -> that player's buddy uid, for every seat the request
    asked a buddy for. A credential, handed back exactly as `seat_uids` is:
    to the caller that created the game and to nobody else (the lobby
    listing's own `advisors` re-answers it later, for the owner alone).

    Left unset - and so absent from the response entirely, under
    `response_model_exclude_unset=True` - for a game with no buddy, so a
    browser written before buddies existed reads exactly what it always
    read."""


@router.post("/games", response_model=NewUiGameResponse, response_model_exclude_unset=True)
def start_game_from_decklists(req: NewUiGame, request: Request) -> dict[str, Any]:
    """Boot a new game from two decklists, right now, from an HTTP body -
    the request-time counterpart to `serve.py`'s `--deck`/`--match`, sharing
    its exact boot path (`decks.load_deck_into_game`): CR 103's registration,
    legality check, sideboard set-aside, seeded shuffles, and opening hands.

    Refuses (400, via the `ValueError`/`DeckIllegal` handlers already
    registered in `app.py`) with the same per-card detail
    `api_client.validate_decklist` reports, rather than booting a broken
    game - both decks are validated before either is registered.

    Games the lobby boots live side by side - starting one never ends
    another; `DELETE /api/lobby/games/{id}` (End game) is the only reset. Refuses
    (409) once `MAX_LIVE_GAMES` lobby games are already live, so the table
    does not grow without bound; ending one from the lobby frees a slot.
    """
    user = oidc.current_user(request)
    problems: dict[str, Any] = {}
    for player in req.players:
        result = api_client.validate_decklist(player.decklist_text)
        if not result["valid"]:
            problems[player.username] = result["errors"]
    if problems:
        raise ValueError(f"decklist(s) failed validation: {problems}")

    for player in req.players:
        if player.buddy and player.ai:
            # A buddy advises a human. An AI seat acts through its own seat
            # uid and never reads the advice ledger, so an advisor minted
            # here would propose into a stream with no reader - and in
            # kubernetes mode it would cost a second full Job (agent plus
            # driver) to do it. `docs/ai_buddy_brief.md` opens by telling the
            # advisor it sits beside a human; this is what makes that true.
            raise ValueError(
                f"{player.username}: a buddy advises a human seat - an ai seat plays "
                "through its own uid and never reads the advice its buddy would write"
            )
    # Always `AgentSettings.default(os.environ)` (2026-09-19): model, effort
    # and thinking budget are a fixed server-side deployment choice now, for
    # an AI seat and a buddy alike - `NewUiPlayer` carries no field a request
    # could use to ask for anything else.
    agents = {
        player.username: AgentSettings.default(os.environ) for player in req.players if player.ai
    }
    # Keyed by uid, never by name - `repository.Advisor`'s own docstring
    # gives the reason (two advisors on one seat, and an advisor on a seat
    # that is itself an AI seat, both collide in a name-keyed map), which is
    # also why this does not go through `register_agent_settings`.
    advisors = {
        uuid4().hex: Advisor(player=player.username, agent=AgentSettings.default(os.environ))
        for player in req.players
        if player.buddy
    }

    # M8: reserve-then-create, never create-then-reserve. `reserve_slot`
    # claims one of `max_live_games()` slots atomically - counting live ui
    # games and every other outstanding reservation - before any of the
    # (real, sometimes slow) work below runs, so no concurrent request can
    # ever slip a `live_ui_games` count under the cap while this reservation
    # is outstanding. This is also what used to be a separate fast,
    # non-atomic pre-check ahead of `load_deck_into_game`'s real work: that
    # pre-check bought nothing `reserve_slot` doesn't already give for free
    # now that the cap decision itself no longer waits on deck construction.
    token = store.reserve_slot(max_live_games())
    if token is None:
        live = store.live_ui_games()
        raise HTTPException(
            status_code=409,
            detail=(
                f"{len(live)} games are already live, the cap is {max_live_games()}; "
                "end one from the lobby first"
            ),
        )
    try:
        # `store.create` is the api half's plain, cap-unaware create (M8:
        # "the api half keeps a plain `create`") - not routed through
        # `api_client.ApiClient`, which pre-cut is still an in-process call
        # to this same module's own functions and has no game-creation
        # method of its own; that seam is for the extraction itself
        # (stage B), not this task.
        match = load_deck_into_game(
            [(player.username, player.decklist_text) for player in req.players],
            seed=req.seed,
        )
        game = match.games[-1]
        game_id = store.create(game, owner_user_id=user.sub if user is not None else None)
        store.attach(token, game_id)
    finally:
        # A no-op once `attach` above already resolved `token` on the
        # success path (`GameRepository.release`'s own docstring) - this is
        # the one call standing between a failed or raised create and a
        # permanently leaked reservation.
        store.release(token)
    uids = new_seat_uids(game)
    store.register_seats(game_id, uids)
    store.register_decklists(
        game_id, {player.username: player.decklist_text for player in req.players}
    )
    store.register_agent_settings(game_id, agents)
    if advisors:
        store.register_advisors(game_id, advisors)
    # Nothing runs CR 103.5 or CR 103.6 here. `Match.start_game` has already
    # left the game resting in `Step.MULLIGAN` (turn 0), and the seats drive it
    # from there through `POST /api/games/{id}/pregame/...` - see `opening.py`
    # for why this used to be a background thread and why it must not be one.
    audit.record(
        game_id,
        "create_game",
        players=[player.username for player in req.players],
        source="ui",
        seed=str(match.seed),
    )
    seat_uids = {name: uid for uid, name in uids.items()}
    ai_players = [player.username for player in req.players if player.ai]
    # The bot seats' pregame backstop. CR 103.5 needs every seat's declaration
    # and CR 103.6 hands the window to one seat at a time, so a bot seat that
    # goes quiet - and the headless `claude` subprocess demonstrably does,
    # exiting once it decides the pregame is "nothing to act on" - strands the
    # human at a panel no button of theirs can move. See `ai_pregame.py`.
    ai_pregame.start(game_id, ai_players)
    # Start each AI seat right away rather than making a human click a second
    # "start" button for a seat they already flagged `ai: true` - the
    # separate `POST .../ai-seat/start` route stays, both for a seat that
    # failed to auto-start and for a lobby rejoining an already-live game.
    # Guarded by `claude_available()` in subprocess mode (the same check
    # `AiSeatRegistry.start` makes internally) so a test posting here with
    # `ai: true` never spawns a real `claude` process just because one
    # happens to be on this machine's PATH.
    mode = ai_seat.ai_seat_mode()
    api_base = ai_seat.api_service_url()
    if mode != "subprocess" or ai_seat.claude_available():
        for player in req.players:
            if not player.ai:
                continue
            ai_seat.registry.start(
                uid=seat_uids[player.username],
                game_id=game_id,
                player=player.username,
                api_base=api_base,
                agent=agents[player.username],
            )
        # The buddies, beside them and through the same registry - the same
        # three mechanisms, the same idempotent start, the same never-raises
        # stance (`AiSeatRegistry.start`). A buddy that cannot start comes
        # back a `fallback`, pollable on `game_seats` for the UI to render;
        # it never fails this request, because the game is fully playable
        # without one.
        for uid, advisor in advisors.items():
            ai_seat.registry.start(
                uid=uid,
                game_id=game_id,
                player=advisor.player,
                api_base=api_base,
                agent=advisor.agent or AgentSettings.default(os.environ),
                role=ai_seat.ROLE_ADVISOR,
            )
    answer: dict[str, Any] = {
        "game_id": game_id,
        "seat_uids": seat_uids,
        "admin_key": store.admin_key(game_id),
        "ai_players": [player.username for player in req.players if player.ai],
        "agents": {name: settings.model_dump() for name, settings in agents.items()},
    }
    if advisors:
        # Set only when there is one, so `response_model_exclude_unset=True`
        # leaves the key out entirely for every game that has no buddy.
        answer["advisor_uids"] = {advisor.player: uid for uid, advisor in advisors.items()}
    return answer


class LobbyGameSummary(BaseModel):
    game_id: str
    players: list[str]
    turn_number: int | None = None
    phase: str | None = None
    is_over: bool
    agents: dict[str, AgentSettings]
    seats: dict[str, str]
    advisors: dict[str, str] = {}
    """player name -> that player's buddy uid, under exactly the rule
    `seats` above follows: the uids the caller already presented in `?uids=`,
    or every one of them for the game's own **owner** and nobody else.

    A uid is a credential, and this is the only route that hands one back
    after creation - which is what makes a buddy survive a reload, and makes
    "start the game on one computer and finish on the other" cover the buddy
    too rather than stranding it. `{}` for a caller with neither claim, and
    for every game that has no buddy."""
    created_at: str | None = None
    unloadable: str | None = None
    """Set only when this game's row exists but this build cannot decode the
    snapshot in it (`repository.GameUnloadable`) - the decoder's own message,
    verbatim, which for the case this exists for is the engine's
    `SchemaVersionMismatch` after `Game.CURRENT_SCHEMA_VERSION` moved past
    what the row was written at.

    Such a game is listed rather than dropped, and listed *fully*: `players`,
    `seats`, `agents` and `created_at` all come from columns that need no
    decoding, so the row still reads as the game the human remembers
    playing. Only `turn_number` and `phase`, which could have come from
    nowhere but the board, are `None`, and `is_over` is `False` (see
    `serialize.unloadable_game_summary` for why that is the honest value and
    why it matches what `MAX_LIVE_GAMES` counts). The lobby renders it
    unopenable and offers End game, which is what actually clears it."""
    recorded_ending: int | None = None
    """The `seq` of this game's `game_over` row (`audit.GAME_OVER_ACTION`),
    when the ledger holds one.

    **Computed only for an `unloadable` row**, and `None` on every other one.
    It is not a second opinion on `is_over`: for a row that decodes,
    `is_over` already answers this off the board, and paying a ledger read
    per game per 1.2s poll to answer it twice is exactly the N+1 the summary
    seam was cut to remove. For a row that does not decode there is no board
    to ask, and this is the only surviving record that the game reached CR
    104.1 - which is what lets the lobby offer that game's review knowing the
    review will actually read (`review.py` rebuilds it from the same ledger),
    instead of either hiding a reachable review or offering a button that
    409s. `None` here means "no ending is recorded", never "the game is still
    going": nothing about an undecodable board is knowable, which is why
    `is_over` stays `False` and the row still counts against
    `MAX_LIVE_GAMES`."""


class LobbyGamesResponse(BaseModel):
    games: list[LobbyGameSummary]


def _recorded_ending(game_id: str) -> int | None:
    """The `seq` of this game's newest `game_over` row, or `None` if the
    ledger holds none - see `LobbyGameSummary.recorded_ending`.

    The bounded read (`recent_records`), never the whole ledger: this is on a
    listing route the browser polls, and a `game_over` row is written at the
    end of a game with very little after it - an undecodable game refuses
    every further action, so the only rows that can follow are the End that
    clears it.
    """
    window = store.recent_records(game_id, RECENT_RECORDS_WINDOW)
    for row in reversed(window):
        if row["payload"].get("action") == audit.GAME_OVER_ACTION:
            seq = row["seq"]
            return int(seq)
    return None


@router.get("/games", response_model=LobbyGamesResponse, response_model_exclude_unset=True)
async def list_lobby_games(request: Request, uids: str | None = None) -> dict[str, Any]:
    """Every lobby-booted game (`store.ui_games()` - not filtered to
    `live_ui_games()`, so a finished-but-not-yet-ended game still shows up for
    End game to clear), newest first, for the lobby's game list.

    Newest first, not the `created_at` ascending order this route used to
    answer with: the game a player wants from this list is almost always the
    one just played or still being played, and oldest-first put that game at
    the *bottom*, past however many older ones came before it. That is the
    condition the lobby UI defect traces to - the rows were indistinguishable
    and the one just played was not even the top row - and `created_at` was
    already a plain, indexable column the sort already keyed off, so this is
    a `reverse=True`, not a new query.

    `uids` is a comma-separated list of seat uids the caller holds, across
    every game it has ever seen - each game's `seats` in the response is
    filtered down to only the uids from that list which are actually seats of
    that game, so a browser never learns a uid that is not already its own.
    `uids` is optional and always was: a caller that presents none simply
    holds no seat capability, which for an authenticated **owner** is now the
    ordinary case rather than a dead end (below).

    OIDC-based user separation: a game this caller may not reach at all - an
    *owned* game (`game_access.py`) belonging to a different bearer token,
    with no uid from `uids` among its seats either - is dropped from the
    list outright, reusing that same `seats` computation as the "do I hold
    any capability for this one" signal rather than adding a second lookup.
    An unowned game (every game before this feature, and every game this
    deployment creates until an OIDC issuer is actually configured) is
    listed exactly as before - this route's one migration guarantee.

    **The owner gets the whole `seats` map, and is the only caller who
    does.** "User must be able to start a game on one computer and finish on
    the other" is the owner's own requirement, and a second browser has an
    empty `localStorage`: it can present no uid, so the `uids` filter would
    hand back `seats: {}` and the row would list but not open. Ownership
    already proves, by a locally-verified signature, that this caller is the
    person who created the game - strictly stronger than holding one of its
    uids - so it is the one identity that may be handed uids it did not
    present. Everyone else keeps the old rule exactly, and an ownerless game
    widens for nobody: the anonymous/dev path (`mise run dev`, the test
    server, and every game already in production) is untouched.
    `tests/test_lobby_cross_device.py` pins both halves.

    2b's T8 (map's S2): this used to build a full `serialize.state_view` per
    game just to pull `turn_number`/`step`/`winner`/`drawn` back out of it -
    the hot N+1 the browser's 1.2s poll paid for on every tick, times however
    many games are live. `api_client.game_summaries` answers every game's
    scalars in one call instead, through the same seam T5 built for deck
    validation - `players` and `agents` stay locally sourced (`store.get`,
    `store.agent_settings`), since neither ever needed the full board view
    either.
    """
    mine = {u for u in (uids or "").split(",") if u}
    user = oidc.current_user(request)
    game_ids = store.ui_games()
    summaries = api_client.game_summaries(game_ids)
    games = []
    for game_id in game_ids:
        # Per game, and around the *only* call here that decodes anything:
        # `ui_games`, `seat_uids`, `owner_user_id`, `agent_settings` and
        # `created_at` are all plain columns. One row this build cannot read
        # therefore costs exactly that row's board scalars and nothing else -
        # before this, it took the whole lobby down with it.
        unloadable: str | None = None
        try:
            game = store.get(game_id)
        except GameUnloadable as exc:
            game, unloadable = None, exc.reason
        summary = summaries.get(game_id)
        if unloadable is None and (game is None or summary is None):
            continue
        all_seats = {name: uid for uid, name in store.seat_uids(game_id).items()}
        seats = {name: uid for name, uid in all_seats.items() if uid in mine}
        all_advisors = {advisor.player: uid for uid, advisor in store.advisor_uids(game_id).items()}
        advisors = {name: uid for name, uid in all_advisors.items() if uid in mine}
        owner = store.owner_user_id(game_id)
        if owner is not None and user is not None and user.sub == owner:
            # The owner, and only the owner, gets every seat of her own game -
            # this is what makes "start on one computer, finish on the other"
            # work at all. A browser she has never used holds nothing in
            # `localStorage`, so it sends no `uids` and `seats` above is empty;
            # without this the row lists but cannot be opened, which is the
            # defect. Nobody else is widened: a caller who is not the owner
            # (another logged-in user, or an anonymous browser holding a uid)
            # keeps the pre-existing guarantee that it never learns a uid it
            # did not already present.
            seats = all_seats
            advisors = all_advisors
        elif owner is not None and not seats:
            continue
        games.append(
            {
                "game_id": game_id,
                # `known_players` for the undecodable case only: the seats
                # and decklists columns still name everyone who sat down, so
                # the row does not read as a game nobody played.
                "players": (
                    known_players(game_id) if game is None else [p.username for p in game.players]
                ),
                "turn_number": summary["turn_number"] if summary else None,
                "phase": summary["phase"] if summary else None,
                "is_over": bool(summary["is_over"]) if summary else False,
                "agents": {
                    name: settings.model_dump()
                    for name, settings in store.agent_settings(game_id).items()
                },
                "seats": seats,
                "advisors": advisors,
                "created_at": store.created_at(game_id),
                "unloadable": unloadable,
                # Only for the row that cannot be decoded - see
                # `LobbyGameSummary.recorded_ending` for why this is not
                # asked of every row.
                "recorded_ending": (None if unloadable is None else _recorded_ending(game_id)),
            }
        )
    games.sort(key=lambda entry: entry["created_at"] or "", reverse=True)
    return {"games": games}


class SeatDetail(BaseModel):
    player: str
    uid: str | None = None
    """This row's credential - present only on a SEAT row, and only the
    entry matching the caller's own `uid` query parameter, or on every seat
    entry for the game's admin key. `None` for every other seat, and for a
    caller presenting nothing. **Never present at all on an advisor row.**

    A seat uid *is* the authority to act as that seat, for the life of the
    game, with no reference to who is logged in (`game_access.py`'s module
    docstring). Handing every one of them to any caller who knew the
    `game_id` therefore made this read a way to *obtain* an acting
    credential - which is exactly how the mtg-buddy advisor, a uid the
    server refuses to let act, could have got hold of its human's seat uid
    and acted as the human instead. Scoped the same way `decklist` below
    already was.

    An advisor's uid is held to the same rule from the other direction, and
    it is the one credential a buddy's whole read-only guarantee rests on:
    an advisor row names the advisor's presence, its player, its agent and
    its liveness, and says nothing about how to authenticate as it. The key
    is left *unset* on such a row rather than set to `None`, so
    `response_model_exclude_unset=True` drops it from the answer entirely
    instead of replying `uid: null`.
    """
    role: Literal["seat", "advisor"] = "seat"
    """What kind of credential this row describes, the same field and the
    same meaning `schemas.SeatLookupResponse.role` carries. Defaulted, and
    left unset on a seat row, so this route's answer for a game with no
    buddy is byte-identical to what it always was. It is declared here so
    the two seat-shaped responses this api has cannot drift into disagreeing
    about how a role is spelled."""
    agent: AgentSettings | None = None
    """This seat's `AgentSettings`, if it is an AI seat - `None` for a human
    one, same as `NewUiGameResponse.agents`."""
    ai_seat: dict[str, Any] | None = None
    """`ai_seat.registry.status(uid)` verbatim, or the same `not_started`
    fallback `GET .../ai-seat/status` used to hand back for an unstarted
    uid - `None` only means this seat's liveness was never tracked at all,
    which in practice never happens once a uid exists.

    Verbatim on a *seat* row. On an advisor row it is put through
    `_advisor_row_status` first, which takes the advisor uid out of it - see
    `uid` above for why that row may not name one."""
    decklist: str | None = None
    """This seat's own decklist text (`store.decklists`) - present only on
    the entry matching the caller's own `uid` query parameter, the same
    "own block only" rule `visibility._redact_player` enforced when this
    field still lived on the game server's state view. Never present with
    no `uid`, or one that opens no seat of this game."""


class GameSeatsResponse(BaseModel):
    seats: list[SeatDetail]


#: Status keys that spell an advisor's uid out in full, dropped whole from an
#: advisor's row rather than redacted: `prompt` is the brief with the uid
#: substituted into it (`ai_seat.build_prompt`) and `command` is the argv that
#: prompt is joined into (`ai_seat.command_for`), both handed back by a
#: `mechanism: "fallback"` start so a human holding the uid can run the session
#: by hand. Without the uid they are a wall of text nobody can act on, and the
#: human who *can* act on them reads them where the uid is already known:
#: `GET .../ai-seat/status?uid=<the advisor uid>`, which is unchanged.
_UID_BEARING_KEYS = ("uid", "prompt", "command")

#: What a redacted uid reads as. Named rather than blank so an operator
#: looking at a buddy's row sees that something was taken out, and why.
_REDACTED = "<advisor uid redacted>"


def _scrub(value: Any, uid: str) -> Any:
    """`value` with every occurrence of the full `uid` replaced - through
    dicts, lists and strings alike, because a status value is not always a
    flat string (`stdout_tail` and `stderr_tail` are lists of process
    output)."""
    if isinstance(value, str):
        return value.replace(uid, _REDACTED)
    if isinstance(value, dict):
        return {key: _scrub(item, uid) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub(item, uid) for item in value]
    return value


def _advisor_row_status(status: dict[str, Any], uid: str) -> dict[str, Any]:
    """One `ai_seat.registry.status` answer with the advisor uid taken out of
    it - keys and quoted occurrences both.

    An advisor's row on this broadly-readable route must carry no advisor uid
    at all (`SeatDetail.uid`), and a status dict names it in more places than
    the obvious one. Two passes, because the leaks are two different shapes:

    - **`_UID_BEARING_KEYS` are dropped.** Every producer names the uid it is
      about (`AiSeatRunner.status`, `K8sSeats.status`'s base, both fallback
      shapes), and a fallback also hands back the `prompt` and the `command`
      it was built from, each with the uid inside it.
    - **Whatever is left is scrubbed of the uid as a substring.** A value this
      module does not compose can still quote the credential: a kubernetes
      `reason` is `could not read pod status: {exc}`, and the exception is
      raised against a label selector of `mtg/seat-uid=<the full uid>`
      (`K8sSeats._pod`), so the uid rides out in the API error's own text. A
      subprocess runner's `stdout_tail` is the agent's own output, which
      quotes its credential freely. Neither key can be enumerated ahead of
      time, so the uid is removed by value rather than by name, which also
      covers the mechanism nobody has written yet.

    Doing it here, on the way out, rather than asking each producer not to say
    it, is what makes the guarantee hold for a status shape this module does
    not own: there is exactly one place a uid can reach an advisor row from,
    and this is it.

    `job` survives on a kubernetes status, uid prefix and all
    (`k8s_seats.job_name` truncates both ids to eight characters, so the
    scrub - which matches the whole uid - leaves it alone): it is what an
    operator greps for, the remaining 96 bits of the uid are not in it, and
    this same route already hands out every *seat* uid in full - so an
    attacker after a credential has a strictly better one to take, and
    dropping the Job name would cost the human the pod name without closing
    anything."""
    kept = {key: value for key, value in status.items() if key not in _UID_BEARING_KEYS}
    if not uid:
        # No credential to scrub for, and `str.replace("")` would splice the
        # marker between every character of every value.
        return kept
    return {key: _scrub(value, uid) for key, value in kept.items()}


@router.get(
    "/games/{game_id}/seats", response_model=GameSeatsResponse, response_model_exclude_unset=True
)
def game_seats(game_id: str, uid: str | None = None) -> dict[str, Any]:
    """2b's T8 (map's S6/S7): per-seat agent settings and liveness, in one
    answer. Replaces three things at once - `serialize._player_view` reading
    lobby-owned `agent_settings` *and* `decklists` on every state poll (S6),
    and the play UI's own separate 1.2s `/ai-seat/status` poll (S7) - with a
    single lobby-owned route the play UI now polls every 5s instead.

    `decklist` moved here rather than being dropped outright: it is still
    genuinely needed (`mtg_agent.driver.app`'s `seat_decklist`, matchup-guide
    selection - see `tests/driver/test_app.py`'s
    `test_the_real_game_server_carries_the_field_seat_decklist_reads`), and
    it is still hidden information (CR 401.2) - so it is redacted the same
    way `visibility._redact_player` redacted it on the old route: present
    only for the seat whose own `uid` the caller presents, absent for every
    other entry and for a caller with no `uid` or an unrecognised one.

    **`uid` is scoped exactly the same way**, and for a stronger reason: a
    seat uid is not a label, it is the authority to act as that seat
    (`SeatDetail.uid`). Handing every seat's uid to every caller made this
    permitted read a credential vending machine - the advisor credential,
    whose entire design is a uid that may read and may not act, could ask
    this route for its human's seat uid and then act as the human, with
    `game_access.py`'s gate never consulted. The `uid` echoed inside the
    `ai_seat` liveness block is redacted with it: it was the same secret,
    spelled twice, and nothing reads it (`ui/store.js` reads `state`,
    `mechanism`, `activity` and the error fields, never `uid`).

    The admin key is the one credential answered in full. It is the
    operator's own - `visibility.ADMIN`, already trusted with the whole
    board - and a harness holding it drives both seats by design.
    """
    if store.get(game_id) is None:
        raise KeyError(f"no game {game_id}")
    agents = store.agent_settings(game_id)
    decklists = store.decklists(game_id)
    operator = bool(uid) and store.is_admin(game_id, uid)
    # Whose own block this caller is entitled to see, resolved from the one
    # credential it presented: its own seat, or - for a buddy reading the
    # deck it advises on - the human whose view its advisor uid opens. An
    # advisor sees exactly what its human sees (`visibility.resolve_viewer`,
    # and `game_access.py` lets every GET through for that reason), and its
    # human's own decklist is part of that; without it the buddy's driver
    # cannot identify the deck it is advising on at all
    # (`mtg_agent.driver.app`'s `seat_decklist`). `None` for a caller that
    # presented nothing, or a uid this game does not know.
    own_block = (
        None
        if uid is None
        else (store.player_for_uid(game_id, uid) or player_for_advisor_uid(game_id, uid))
    )
    seats = []
    for seat_uid, player in store.seat_uids(game_id).items():
        status = ai_seat.registry.status(seat_uid)
        own = operator or (uid is not None and uid == seat_uid)
        if status is not None and not own and "uid" in status:
            status = {**status, "uid": None}
        seats.append(
            {
                "player": player,
                "uid": seat_uid if own else None,
                # `.model_dump()`, not the bare `AgentSettings` instance: T1's
                # own lesson (`MEMORY.md`) - `response_model_exclude_unset=
                # True` tracks each *response* instance's own
                # `model_fields_set`, and a model FastAPI can serialize
                # as-is without reconstructing it keeps whatever that
                # instance's constructor happened to set explicitly
                # (`AgentSettings.default()` only ever names `model=`).
                # Handing over a plain dict with every key present forces
                # FastAPI to validate a fresh instance from it, marking all
                # three fields set - the same fix `_player_view` used to get
                # by construction when this field lived there.
                "agent": agents[player].model_dump() if player in agents else None,
                "ai_seat": status
                if status is not None
                else {"uid": seat_uid if own else None, "mechanism": None, "state": "not_started"},
                # `own_block`, not `own`: `decklist` is still never widened to
                # the admin key (`own_block` resolves a seat or an advisor
                # credential, never `is_admin`), so it stays exactly as scoped
                # as it was documented to be - the one addition is the buddy
                # reading the deck it advises on, which is its human's own
                # block and nothing more.
                "decklist": decklists.get(player) if player == own_block else None,
            }
        )
    # The buddies, after the seats and WITHOUT their uids - see
    # `SeatDetail.uid`. `role` is what tells the two kinds of row apart, and
    # appending them last also keeps the first row for any given player the
    # seat row, which is what `mtg_agent.driver.app`'s `seat_decklist` reads.
    for advisor_uid, advisor in store.advisor_uids(game_id).items():
        status = ai_seat.registry.status(advisor_uid)
        seats.append(
            {
                "player": advisor.player,
                "role": ai_seat.ROLE_ADVISOR,
                # From the advisor's own entry, never the name-keyed
                # `agent_settings` map - `NewUiPlayer.buddy_agent` says why.
                "agent": None if advisor.agent is None else advisor.agent.model_dump(),
                "ai_seat": _advisor_row_status(status, advisor_uid)
                if status is not None
                else {"mechanism": None, "state": "not_started", "role": ai_seat.ROLE_ADVISOR},
            }
        )
    return {"seats": seats}


class EndGameResponse(BaseModel):
    game_id: str
    players: list[str]
    seats_invalidated: int
    advisors_invalidated: int = 0
    """How many buddy uids this game had, all of them released and
    invalidated with the seats (`lobby.reset_game`). Counted separately
    rather than folded into `seats_invalidated`, which means seats and is
    read that way by everything that already displays it."""
    watcher_stopped: bool
    ai_seats_released: list[dict[str, Any]]
    """Per-uid release detail from `ai_seat.registry.release` - shape varies
    by mechanism (`subprocess`/`wrapper`/`kubernetes`), and `lobby.py` (out
    of this task's file list) is what would need to change to make it a
    named component; left as the same honest `dict[str, Any]` T1 allowed for
    a genuinely dynamically-keyed field rather than forcing a wrong shape."""
    deleted: bool


@router.delete(
    "/games/{game_id}", response_model=EndGameResponse, response_model_exclude_unset=True
)
def end_lobby_game(game_id: str, uid: str | None = None, key: str | None = None) -> dict[str, Any]:
    """End game: the only reset there is now. `uid` must be a seat uid of
    this game, or `key` this game's admin key - anything else is refused (404)
    rather than letting an arbitrary caller end somebody else's game.

    **A game this build cannot decode can still be ended, and that is not
    incidental.** `_live_ui_game_ids` counts an undecodable live row against
    `MAX_LIVE_GAMES` (see its own note on why), so this route is the one
    thing standing between that decision and a lobby wedged forever. Nothing
    below it needs the board: the credential check reads the seats and
    admin-key columns, and `lobby.reset_game`'s delete is an `UPDATE`. So
    the existence check refuses to be the thing that fails - a row that is
    there but unreadable is *exactly* what this caller is asking to get rid
    of.
    """
    try:
        exists = store.get(game_id) is not None
    except GameUnloadable:
        exists = True
    if not exists:
        raise KeyError(f"no game {game_id}")
    allowed = (uid is not None and store.player_for_uid(game_id, uid) is not None) or (
        key is not None and store.is_admin(game_id, key)
    )
    if not allowed:
        raise KeyError("that uid is no seat in this game")
    return lobby.reset_game(game_id, reason="ended from the lobby")
