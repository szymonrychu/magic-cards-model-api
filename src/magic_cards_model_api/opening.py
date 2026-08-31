"""The mulligan process (CR 103.5) and the opening-hand actions (CR 103.6),
run against a game that is *already* in the session store.

Until this module existed, the API never ran either one. `decks.py`'s
`load_deck_into_game` called the engine's `Match.start_game` - which deals the
opening hands and stops, deliberately, because CR 103.5's declaration sequence
is the players' - and nothing ever called `Match.run_mulligans`. So every game
played through `/play/` or the MCP seat tools began with an unkept seven and
CR 103.6's "some cards allow a player to take actions with them from their
opening hand" never fired at all: a Leyline in an opening hand simply sat
there.

**Why this is a separate step rather than a line inside `load_deck_into_game`.**
CR 103.6's actions are put to their player through `Game.ask`, which is the
one path that consults `Game.decisions.provider` - this API's per-game
`DecisionMailbox` (`decisions.py`). That mailbox is created by
`store.create`, and the seat uid a player needs in order to answer anything is
minted right after it. Run the sequence inside `load_deck_into_game` and both
are still in the future: the question would be asked of a game that has no
mailbox, of a seat whose credential has not been handed out, from inside the
very request whose response carries that credential. The question could only
ever time out. So the order is: start the game, register it and its seats,
*then* run CR 103.5 and CR 103.6 - and run them on a thread of their own, so
the creating request can return the seat uids to the player who is about to be
asked.

**The lock.** The thread holds `store.locked(game_id)` for the whole sequence,
so no mutating endpoint can act on a game whose opening hands are not settled
yet - a seat that tries simply queues behind it, exactly as two mutating
requests already queue behind each other. Reads (`GET /api/games/{id}`) do not
take that lock, which is what lets the deciding seat poll, see the pending
CR 103.6 question, and answer it through `POST /api/games/{id}/decide` - an
endpoint that touches only the mailbox, never the lock this thread is holding.
That is `decisions.py`'s deadlock invariant, unchanged and relied on here.

**The mulligan declaration itself is not live, and cannot be made live from
this repo.** CR 103.5's question goes through `machines/decisions.py`'s
`ask_mulligan`, which consults `DecisionScript.take` and then `default_answer`
and never `DecisionScript.provider`. No mailbox is published to, so no seat can
be shown the question and `POST .../decide` has nothing to answer. Wiring it up
is an engine change (`ask_mulligan` would have to route through `Game.ask` the
way `Game.take_opening_hand_actions` already does), and the engine is not this
repo's to change. Until it is made, every seat's declaration takes the engine's
own documented default for an unscripted player - keep the hand as dealt - and
that is recorded in the audit log as `mulligans_settled` with
`live: false`, so a transcript never implies a player chose to keep.

Every CR number in this module was verified against the Comprehensive Rules
with `knowledge_search` in the session that wrote it.
"""

from __future__ import annotations

import threading

from magic_cards_model.models import Game, Match

from . import audit
from .store import store


def run_opening_sequence(game_id: str, match: Match, game: Game) -> None:
    """Settle `game`'s opening hands: CR 103.5, then CR 103.6.

    Both are the engine's own `Match.run_mulligans`, which walks CR 103.5's
    declaration rounds ("First, the starting player declares whether they will
    take a mulligan. Then each other player in turn order does the same.") and
    finishes by calling `Game.take_opening_hand_actions` for CR 103.6 ("Once
    the mulligan process (see rule 103.5) is complete, the starting player may
    take any such actions in any order.").

    Holds the game's mutation lock throughout - see the module docstring - and
    records what happened either way, so an operator reading the audit log can
    tell a kept seven from a mulliganed one, and a CR 103.6 action taken from
    one declined.
    """
    try:
        with store.locked(game_id) as stored:
            del stored  # the lock is the point; `game` is that same object
            match.run_mulligans(game)
    except KeyError:
        # The game was deleted between creation and this thread starting.
        # Nothing to settle, and nothing to log against an id that is gone.
        return
    finally:
        store.opening_done(game_id).set()

    hands = ", ".join(f"{player.username}={len(player.hand)}" for player in game.players)
    mulligans = ", ".join(f"{player.username}={player.mulligans_taken}" for player in game.players)
    audit.record(
        game_id,
        "mulligans_settled",
        hands=hands,
        mulligans=mulligans,
        # False until `machines/decisions.py`'s `ask_mulligan` routes through
        # `Game.ask`: no seat was ever shown CR 103.5's declaration, so every
        # entry above is the engine's default for an unscripted player, not a
        # choice anybody made. See the module docstring.
        live=False,
    )
    # No turn has begun, so nothing can have reached the battlefield any other
    # way than CR 103.6a's "begin the game with that card on the battlefield".
    began_on_battlefield = ", ".join(card.name for card in game.battlefield)
    audit.record(
        game_id,
        "opening_hand_actions",
        # CR 103.6's questions *are* live: they go through `Game.ask`, which
        # consults this API's mailbox, so a seat really was asked and really
        # could have answered - unlike CR 103.5's declaration above.
        live=True,
        battlefield=began_on_battlefield or "none",
        notices=", ".join(notice.text for notice in game.opening_hand_notices) or "none",
    )


def start_opening_sequence(game_id: str, match: Match, game: Game) -> threading.Thread:
    """Run `run_opening_sequence` on a daemon thread and return it.

    The creating request returns immediately, seat uids and all, while CR
    103.5 and CR 103.6 settle behind it - which is the only ordering in which
    the seat that CR 103.6 asks can actually have the credential it needs to
    answer. Callers that must see the settled state (the boot summary, tests)
    wait on `store.opening_done(game_id)` rather than joining this thread, so
    they never depend on holding a reference to it.

    Daemon, because every wait inside is already bounded
    (`DecisionMailbox.wait_for_answer` times out twice over) but a process
    shutting down should not be held up by an unanswered question either way.
    """
    thread = threading.Thread(
        target=run_opening_sequence,
        args=(game_id, match, game),
        name=f"opening-sequence-{game_id}",
        daemon=True,
    )
    thread.start()
    return thread


def wait_for_opening_sequence(game_id: str, timeout: float = 10.0) -> bool:
    """Block until this game's CR 103.5/103.6 sequence has finished.

    Returns whether it finished within `timeout`. For callers that want the
    settled opening hand rather than the dealt one - `serve.py`'s boot
    summary, and tests that assert on a hand.
    """
    return store.opening_done(game_id).wait(timeout)


__all__ = [
    "run_opening_sequence",
    "start_opening_sequence",
    "wait_for_opening_sequence",
]
