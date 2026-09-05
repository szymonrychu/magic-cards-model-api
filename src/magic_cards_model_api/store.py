"""The in-memory session store: game id -> engine Game. Nothing persists.

Alongside each game the store keeps that game's seat credentials - a secret
uid per player - so a caller can prove which seat is theirs without the
opponent ever being able to claim it. The uids are generated when the game is
created, handed to whoever dispatches the players, and never appear in any
state view.

The store also mints one admin key per game, handed only to whoever created
it (alongside the seat uids, in the same response) - the operator's own
credential, good for the full, unfiltered view a harness needs to build and
verify a scenario. It is never returned again after creation, exactly like a
seat uid, and it never appears in any state view either.

Each game also gets, at creation, a mutation lock and a `DecisionMailbox`
(`decisions.py`). The lock closes a latent race this store used to leave
open: `get()` used to hand out the shared `Game` with nothing stopping two
requests from mutating it at once. `lock()` is that fix - every mutating
endpoint holds it for the request's whole body. The mailbox is what
`Game.ask` talks to when nobody scripted an answer; see `decisions.py`'s
module docstring for why the endpoint that answers it must never take this
same lock.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from magic_cards_model.models import Game

from .agent_settings import AgentSettings
from .decisions import DecisionMailbox, GameDecisionProvider


class GameStore:
    """Every game the API is currently holding. A restart wipes it."""

    def __init__(self) -> None:
        self._games: dict[str, Game] = {}
        self._seats: dict[str, dict[str, str]] = {}  # game_id -> uid -> username
        self._admin_keys: dict[str, str] = {}  # game_id -> admin key
        self._locks: dict[str, threading.Lock] = {}  # game_id -> its mutation lock
        self._mailboxes: dict[str, DecisionMailbox] = {}  # game_id -> its pending decision
        self._decision_timeouts: dict[str, list[dict]] = {}  # game_id -> defaulted decisions
        self._decklists: dict[str, dict[str, str]] = {}  # game_id -> username -> decklist text
        # game_id -> username -> that seat's AgentSettings. Only AI seats have
        # an entry - a human seat is never given one, same as `_ai_players`.
        self._agent_settings: dict[str, dict[str, AgentSettings]] = {}
        self._ai_players: dict[str, set[str]] = {}  # game_id -> usernames driven by a bot
        # game_id -> username -> the pregame moves something made *for* that
        # seat rather than the seat making them itself. See
        # `record_automatic_pregame`.
        self._automatic_pregame: dict[str, dict[str, set[str]]] = {}
        # The games the play UI's lobby booted, oldest first. Only those are
        # ever reset through `DELETE /api/ui/games/{id}` (`lobby.py`): a game
        # booted by `serve.py --match` or by `POST /api/games` belongs to
        # whoever booted it, and the browser ending a lobby game must not
        # touch somebody else's concurrent one.
        self._ui_games: list[str] = []
        self._lock = threading.Lock()

    def create(self, game: Game) -> str:
        game_id = uuid4().hex
        mailbox = DecisionMailbox()
        game.decisions.provider = GameDecisionProvider(mailbox=mailbox, game_id=game_id)
        with self._lock:
            self._games[game_id] = game
            self._admin_keys[game_id] = uuid4().hex
            self._locks[game_id] = threading.Lock()
            self._mailboxes[game_id] = mailbox
        return game_id

    def register_ui_game(self, game_id: str) -> None:
        """Record that the play UI's lobby booted this game.

        `lobby.reset_previous_games` resets exactly these and nothing else, so
        the marker is set only by `POST /api/ui/games` - never inferred.
        """
        with self._lock:
            if game_id not in self._ui_games:
                self._ui_games.append(game_id)

    def ui_games(self) -> list[str]:
        """The lobby-booted games still in the store, oldest first."""
        with self._lock:
            return [game_id for game_id in self._ui_games if game_id in self._games]

    def live_ui_games(self) -> list[str]:
        """The lobby-booted games that have not ended yet, oldest first.

        `Game.is_over` (CR 104.1: a winner or a draw) is what separates a game
        still worth a seat at the table from one that only hasn't been ended
        from the lobby yet - this is what `MAX_LIVE_GAMES` counts against, so
        a finished game does not itself block a new one.
        """
        with self._lock:
            return [
                game_id
                for game_id in self._ui_games
                if game_id in self._games and not self._games[game_id].is_over
            ]

    def register_decklists(self, game_id: str, decklists: dict[str, str]) -> None:
        """Record the decklist text each seat was booted from: username -> text.

        Kept beside the game rather than on it, for the same reason the seat
        uids are: `Game` is the engine's model of a game in progress and a
        registered decklist is not part of one. The engine's `Match.decks`
        does hold the parsed lists, but this store keys everything by
        `game_id` and never sees the `Match` - `decks_api`/`serve` hand it the
        `Game` alone - so the text is recorded here at boot instead.

        Only games booted from decklists have any (`POST /api/games` builds a
        board with no decks at all), and a game with none simply has nothing
        to report.
        """
        with self._lock:
            self._decklists[game_id] = dict(decklists)

    def decklists(self, game_id: str) -> dict[str, str]:
        """username -> decklist text, for a game booted from decklists.

        Full information: `serialize.state_view` hands each seat only its own
        entry, and `visibility.redact_view` is what enforces that (CR 100.2a's
        "each player creates their own deck" is knowledge of one's own deck,
        not of the opponent's).
        """
        with self._lock:
            return dict(self._decklists.get(game_id, {}))

    def register_agent_settings(self, game_id: str, agents: dict[str, AgentSettings]) -> None:
        """Record each AI seat's `AgentSettings`: username -> settings.

        Kept beside the game for the same reason `register_decklists` is:
        `Game` is the engine's model of a game in progress, and which Claude
        Code model, effort and thinking budget play a seat is a fact about
        this deployment, not about the game. Only AI seats get an entry -
        `decks_api.start_game_from_decklists` builds this dict from `player.ai`
        already, so a human seat is simply absent rather than recorded as
        having none.
        """
        with self._lock:
            self._agent_settings[game_id] = dict(agents)

    def agent_settings(self, game_id: str) -> dict[str, AgentSettings]:
        """username -> `AgentSettings`, for this game's AI seats only."""
        with self._lock:
            return dict(self._agent_settings.get(game_id, {}))

    def register_ai_players(self, game_id: str, players: list[str]) -> None:
        """Record which seats are meant to be driven by a bot rather than a
        human at a browser.

        Kept beside the game for the same reason the seat uids are: "this seat
        has a subprocess playing it" is a fact about *this deployment*, not
        about the game, and `Game` must not learn it. The one thing the server
        does with it is `ai_pregame.py`'s backstop, which will make a seat's
        own CR 103.5 declaration for it - and must therefore never be able to
        confuse a bot seat with the human's.
        """
        with self._lock:
            self._ai_players[game_id] = set(players)

    def ai_players(self, game_id: str) -> set[str]:
        """The usernames registered as bot-driven. Empty for every game that
        did not say so - a human seat is never inferred into this set."""
        with self._lock:
            return set(self._ai_players.get(game_id, set()))

    def record_automatic_pregame(self, game_id: str, player: str, kind: str) -> None:
        """Remember that `kind` (`"mulligan"` or `"opening_hand"`) was taken
        *for* `player` by `ai_pregame.py`'s backstop rather than by the seat.

        This is what lets a late declaration from the seat itself be refused
        with the truth instead of the engine's bare "has already kept a hand".
        The seat did not already keep a hand; something kept one on its
        behalf, and those are different sentences to an agent whose only
        feedback is the response body. See `opening.py`'s `PregameOverruled`.
        """
        with self._lock:
            self._automatic_pregame.setdefault(game_id, {}).setdefault(player, set()).add(kind)

    def automatic_pregame(self, game_id: str, player: str) -> set[str]:
        """The pregame moves the backstop made for this seat, if any."""
        with self._lock:
            return set(self._automatic_pregame.get(game_id, {}).get(player, set()))

    @contextmanager
    def locked(self, game_id: str) -> Iterator[Game]:
        """Hold `game_id`'s mutation lock for a whole request, yielding the
        `Game`. Raises `KeyError` for an unknown game before taking any lock.

        This is the lock the module docstring describes; `decisions.py`'s
        `GameDecisionProvider.decide` runs *inside* a call to this - a request
        parked here, deep in `Game.ask`, is exactly what a decision answer
        must never wait behind (see that module).
        """
        with self._lock:
            game = self._games.get(game_id)
            lock = self._locks.get(game_id)
        if game is None or lock is None:
            raise KeyError(f"no game {game_id}")
        with lock:
            yield game

    def mailbox(self, game_id: str) -> DecisionMailbox | None:
        """This game's pending-decision mailbox, or `None` if unknown.

        Looked up without `locked()` and without touching `Game` at all: the
        `decide` endpoint uses only this, which is the whole point of keeping
        the mailbox's lock separate from the game's.
        """
        with self._lock:
            return self._mailboxes.get(game_id)

    def record_decision_timeout(self, game_id: str, record: dict) -> None:
        """A decision that timed out and took the engine's default, for this
        game - appended so `decision_timeouts` (`serialize.state_view`) can
        show it in the game state itself, not only the audit log. A default
        answer produces a plausible-looking transcript of a game that was
        never actually played through that choice, so it belongs where every
        reader of the current state sees it, not only where someone thought
        to look at history. Called from `decisions.GameDecisionProvider`,
        itself lazy-importing this module (see that call site's own comment)."""
        with self._lock:
            self._decision_timeouts.setdefault(game_id, []).append(record)

    def decision_timeouts(self, game_id: str) -> list[dict]:
        """Every decision timeout recorded for this game so far, oldest first."""
        with self._lock:
            return list(self._decision_timeouts.get(game_id, []))

    def admin_key(self, game_id: str) -> str | None:
        """This game's admin key, to hand to whoever just created it."""
        with self._lock:
            return self._admin_keys.get(game_id)

    def is_admin(self, game_id: str, key: str) -> bool:
        """Whether `key` is this game's admin key. A missing key is never one."""
        with self._lock:
            return bool(key) and self._admin_keys.get(game_id) == key

    def register_seats(self, game_id: str, uids: dict[str, str]) -> None:
        """Record `uids` (uid -> username) as this game's seat credentials."""
        with self._lock:
            self._seats[game_id] = dict(uids)

    def seat_uids(self, game_id: str) -> dict[str, str]:
        """uid -> username, for whoever is dispatching the players."""
        with self._lock:
            return dict(self._seats.get(game_id, {}))

    def player_for_uid(self, game_id: str, uid: str) -> str | None:
        """Whose seat this uid is, or None when the uid is not in this game."""
        with self._lock:
            return self._seats.get(game_id, {}).get(uid)

    def get(self, game_id: str) -> Game | None:
        with self._lock:
            return self._games.get(game_id)

    def delete(self, game_id: str) -> bool:
        with self._lock:
            self._seats.pop(game_id, None)
            self._admin_keys.pop(game_id, None)
            self._locks.pop(game_id, None)
            self._mailboxes.pop(game_id, None)
            self._decision_timeouts.pop(game_id, None)
            self._decklists.pop(game_id, None)
            self._agent_settings.pop(game_id, None)
            self._ai_players.pop(game_id, None)
            self._automatic_pregame.pop(game_id, None)
            if game_id in self._ui_games:
                self._ui_games.remove(game_id)
            return self._games.pop(game_id, None) is not None

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._games)


def new_seat_uids(game: Game) -> dict[str, str]:
    """A fresh secret uid for each of the game's players: uid -> username."""
    return {uuid4().hex: player.username for player in game.players}


store = GameStore()
