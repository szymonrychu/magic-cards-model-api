"""One MCP toolset for the whole game: every tool takes the caller's seat uid.

Agents are dispatched with a secret uid each (printed by `serve` at boot and
handed out by whoever launches the players). The uid is the only way to claim
a seat - tools carry it as their first parameter, an unknown uid can neither
act nor see anything, and the hidden-information boundary is keyed off the
seat the uid opens: the opponent's hand and the contents of every library
never cross it. Everything else - battlefield, stack, graveyards, exile,
life, pools, combat - is public exactly as it is at a real table.

The filtering itself lives on the server (`visibility.py`), not here: every
call that can return a state view carries this seat's uid as a query
parameter, and the API answers already redacted. This module never sees the
unredacted state - it could not leak it if it tried, which is the point:
before, this file computed the correct answer and anything that talked to
the API directly (skipping this process) got the unfiltered one instead.

A seat has exactly two priority verbs, `pass` and `pass_until`, and that is
the whole of what priority offers: CR 117.1 says the player with priority "may
cast spells, activate abilities, and take special actions", and CR 117.3d says
a player who chooses none of those passes. What follows from a pass is not a
move anybody makes - CR 117.4 resolves the top of the stack, or ends the step,
once all players have passed in succession, and CR 703.2 says turn-based
actions "are not controlled by any player" - so there is deliberately no tool
here for resolving the stack, stepping the turn on, or putting the turn marker
somewhere. `pass_until` is the same verb run to a destination, with the stop
conditions that decide when it hands the board back.

Every action tool below - `cast`, `play_land`, `suspend`, `plot`, `foretell`,
`cycle`, `activate`, `declare_attackers`, `declare_blockers`, `decide`,
`pass`, `pass_until` - is "one call, one meaningful action":
it performs the action, checks once for this seat's next moment to act, and
returns the outcome plus whatever state that check saw, all in one answer.

That answer carries three things and not one, because a seat needs all three
after every move and used to spend a round trip - and so a whole inference
cycle, which is what a seat's turn latency is actually made of - on each:
`state` (the board it left behind), `available_actions` (what is legal in it,
the engine's own enumeration) and `recent_log` (the tail of what has actually
happened, both seats, allowed and refused). `battlefield_state` and
`wait_for_priority` carry `recent_log` too. The pair this closes is the one
the driver prompt used to spell out in as many words - "read `my_state`, read
`game_log`", `my_state` being what `battlefield_state` was called then - and
which a live seat pod was observed doing mid-game: two calls for one question.
`game_log` remains for the whole history and for the full board snapshot each
entry carries; `seatkit.RECENT_LOG_ENTRIES` bounds the tail, and its `omitted`
count is what tells a seat the tail dropped something.
This never blocks - MCP protocol revision 2026-07-28 removed resumability
(SEP-2577), so a tool call that holds the request open waiting for something
to happen loses it outright if the underlying stream breaks mid-wait -
poll-and-return instead (`seatkit.block_until_wake`'s own docstring): call
again to keep checking. A decision pending for the caller - whether this
very call raised it or something else did - arrives shaped `input_required`
(`InputRequiredResult`, `result_type: "input_required"` on the wire) rather
than as plain text a caller could skim past. The whole flow (wake
conditions, `hold_priority`, the no-deadlock invariant, the
`input_required` shaping) lives in `seatkit.run_action`/`mcp._tool_result`,
shared with `seat_cli.py` - see that module's docstring.

Whether the harness auto-passes priority on the seat's behalf is NOT uniform,
and the tools that differ say so in capitals in their own docstrings:

- `play_land`, `suspend`, `plot`, `foretell` and `cycle` **hold priority by
  default** (`hold_priority=True`). The first four are special actions, and
  CR 116.3 says "if a player takes a special action, that player receives
  priority afterward"; cycling is an activated ability (CR 702.29a) that
  earns the same treatment through CR 117.3c. Releasing after one of these
  throws away the rest of the seat's own main phase, which is exactly the bug
  that put this paragraph here.
- `activate` **holds priority whenever the ability turned out to be a mana
  ability**, whatever `hold_priority` said - a mana ability does not use the
  stack (CR 605.3b) and the auto-pass could end the step (CR 500.2) and empty
  the mana it just made (CR 500.5). See `seatkit.added_mana`.
- everything else releases priority unless told to hold, which is right for
  them: they use the stack, and releasing is how what you just did resolves.

This process is spawned once for the life of a whole playtest session and
never reloads its code - editing this file after that does not change what a
running process serves. The API, by contrast, is rebooted once per game (a
fresh `uv run python -m magic_cards_model_api.serve`), so this process must
outlive the API restarting underneath it: every tool below is wrapped in
`_guarded` (see there), which checks `seatkit.PROTOCOL_VERSION` and basic
reachability before each call and turns either problem into a clear, returned
error rather than an exception - a stale-protocol mismatch or an unreachable
API refuses that one call loudly instead of taking this process down with it.

Run one server, not per player (`--transport {stdio,http}`, default `http`):

    uv run python -m magic_cards_model_api.mcp [--game <id>] [--transport stdio|http]
        [--api http://127.0.0.1:8000] [--host 127.0.0.1] [--port 8100]

`http` (the default) serves the MCP SDK's stateless streamable-HTTP ASGI app
and, with no --game, resolves each call's game from its own seat uid (`GET
/api/seats/{uid}`) - one process can answer for several live games. `stdio`
is the historical per-game-subprocess shape (`deploy/mcp.base.json`'s
docker-compose config uses this explicitly); with no --game there it falls
back to "the one live game" instead. Every tool returns the API's JSON
answer verbatim, engine refusal strings included.
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import sys
from collections.abc import Callable
from typing import Any

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import InputRequiredResult
from mcp_types.version import MODERN_PROTOCOL_VERSIONS

from . import seatkit
from .playscripts import script_tools
from .seatkit import (
    DEFAULT_API,
    NO_SUCH_SEAT,
    Seat,
    Seats,
    _parse_ids,
    _parse_pairs,
    card_reference,
    parse_convoke,
    parse_optional_costs,
    run_action,
)


def cancel_probe(ctx: Any) -> Callable[[], bool]:
    """Whether this tool call's request has been cancelled, as a probe a wait
    loop can read between polls.

    An interrupt in the seat's claude cancels its in-flight MCP call
    (`notifications/cancelled`), and the SDK sets the request's
    `cancel_requested` - but a sync tool runs on a worker thread nothing can
    cancel, so without this a cancelled `wait_for_priority` kept polling for
    its whole bound. Outside a request there is nothing to cancel."""
    try:
        event = ctx.request_context.cancel_requested if ctx is not None else None
    except Exception:  # noqa: BLE001 - no request in flight, nothing to cancel
        event = None
    if event is None:
        return lambda: False
    return event.is_set


def _guarded(seats: Seats):
    """Wrap one tool function so the API restarting underneath this
    long-lived process - once per playtest game, see the module docstring -
    can never kill it. Before the call: `seatkit.api_trouble` checks
    reachability and `PROTOCOL_VERSION`, going through `seats.http` so tests
    that monkeypatch `httpx.Client` are honoured. Around the call: any
    `httpx.HTTPError` raised mid-request (the API dropping away between the
    check above and the actual call) is caught the same way rather than
    propagating out of the tool and taking the process down. Either problem
    is returned as a plain string - a normal tool answer - never raised.
    """

    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> str:
            trouble = seatkit.api_trouble(seats.http, seats.api)
            if trouble:
                return trouble
            try:
                return fn(*args, **kwargs)
            except httpx.HTTPError as exc:
                return f"the game server at {seats.api} became unreachable mid-call: {exc}"

        return wrapper

    return decorate


#: `battlefield_state`'s section vocabulary: the name a seat asks for, and
#: the top-level keys of the state view it selects. The names ARE the view's
#: own keys (`serialize.state_view`, after `visibility.redact_view`) rather
#: than a prettier parallel vocabulary, so what a seat asks for is spelled
#: exactly like what it reads back and there is nothing to translate in
#: either direction. `exile` is the one section covering two keys, because
#: `exile_pile` is a filtered *view* of `exile` (`serialize.state_view`) and
#: not a second zone - asking for the pile without the pile it is drawn from
#: would be asking for a shadow.
SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "battlefield": ("battlefield",),
    "hands": ("hands",),
    "graveyards": ("graveyards",),
    "exile": ("exile", "exile_pile"),
    "command": ("command",),
    "stack": ("stack",),
    "combat": ("combat",),
    "players": ("players",),
    "play_permissions": ("play_permissions",),
    "pregame": ("pregame",),
    "pending_decision": ("pending_decision",),
    "recent_log": ("recent_log",),
    "known_cards": ("known_cards",),
    "library_contents": ("library_contents",),
    "sideboard_contents": ("sideboard_contents",),
}

#: Sections left out of the default whole board and served only when named.
#: Each is the asking seat's own library or sideboard as an unordered
#: `{card, count}` list (CR 401.2 keeps the order from everyone), fetched with
#: the api's `?include=contents` - nothing a seat needs on every read, so no
#: answer pays for them unless it asked.
ON_REQUEST_SECTIONS: tuple[str, ...] = ("library_contents", "sideboard_contents")

#: Sections whose entries are card views carrying whose they are - the two
#: shared zones, the command zone, and the stack. `side` filters these on
#: `controller`, falling back to `owner` for a card view that has no
#: controller of its own.
_CARD_LIST_KEYS = ("battlefield", "exile", "exile_pile", "command", "stack")

#: Sections shaped `{username: [...]}` - `side` filters these by key.
_BY_USERNAME_KEYS = (
    "hands",
    "graveyards",
    "play_permissions",
    "library_contents",
    "sideboard_contents",
)

#: What `side` accepts. Deliberately not usernames: the caller already
#: identified itself with `player_uid`, and a mirror match - both seats on
#: the same decklist, the same card names on both boards - is exactly where
#: naming a side by hand goes wrong. `mine` is whoever that uid opens,
#: `opponent` is every other seat, and the answer's `focus` block says which
#: usernames each one resolved to so the reading is never a guess.
_SIDES = ("both", "mine", "opponent")


def _owner_of(card: dict[str, Any]) -> str | None:
    return card.get("controller") or card.get("owner")


def _narrow_to_side(view: dict[str, Any], allowed: set[str] | None) -> dict[str, Any]:
    """`view`, keeping only what belongs to a seat in `allowed` (`None` for
    every seat - the `side="both"` default, which returns `view` untouched).

    Only sections whose entries *say* whose they are get narrowed. `combat`
    is not: an attack is a relationship between the two sides, so there is no
    honest half of it to keep. Neither are `pregame`, `pending_decision`,
    `recent_log` or the spine - none of them is one seat's property.
    """
    if allowed is None:
        return view
    narrowed = dict(view)
    for key in _CARD_LIST_KEYS:
        cards = narrowed.get(key)
        if isinstance(cards, list):
            narrowed[key] = [card for card in cards if _owner_of(card) in allowed]
    for key in _BY_USERNAME_KEYS:
        block = narrowed.get(key)
        if isinstance(block, dict):
            narrowed[key] = {name: value for name, value in block.items() if name in allowed}
    players = narrowed.get("players")
    if isinstance(players, list):
        narrowed["players"] = [player for player in players if player.get("username") in allowed]
    known = narrowed.get("known_cards")
    if isinstance(known, dict):
        narrowed["known_cards"] = {
            knower: _known_block_for(block, allowed) for knower, block in known.items()
        }
    return narrowed


def _known_block_for(block: Any, allowed: set[str]) -> Any:
    """One `known_cards` entry, keeping only records of cards owned by a
    seat in `allowed`. The entry is keyed by the seat that *knows*, so the
    side a record belongs to is the card's own owner, not that key."""
    if not isinstance(block, dict):
        return block
    return {
        "hand": [card for card in block.get("hand") or () if _owner_of(card) in allowed],
        "library": [
            record
            for record in block.get("library") or ()
            if _owner_of(record.get("card") or {}) in allowed
        ],
        "sideboard": [card for card in block.get("sideboard") or () if _owner_of(card) in allowed],
    }


def _select_sections(view: dict[str, Any], wanted: list[str]) -> dict[str, Any]:
    """`view` with every section outside `wanted` removed.

    Removal, never selection: a key claimed by no section at all - the game
    id, the turn, the step, the phase, who is active, who has priority, who
    won, and `you` - is always kept. Those are what tell a seat *which* board
    it is looking at, they cost nothing to carry, and an answer that dropped
    them would be a board a seat could act on without knowing whose turn it
    is.
    """
    dropped = {
        key for section, keys in SECTION_KEYS.items() if section not in wanted for key in keys
    }
    return {key: value for key, value in view.items() if key not in dropped}


#: The sections of the board an *action* tool's answer keeps. Everything else
#: - the battlefield, both hands, both graveyards, exile and the command zone -
#: is `battlefield_state`'s job, and re-serialising it after every single
#: action is what made one `cast` answer 25,397 tokens: more than the seat's
#: own client would read at all, so the agent never saw the refusal the engine
#: had spelled out for it and spent four identical retries and a whole turn
#: guessing from the board instead.
#:
#: What survives is what an action's own answer is *about*: the spine
#: (`_select_sections` keeps the game id, turn, step, phase, active player,
#: priority holder and `you` unconditionally), the stack the cast just changed,
#: combat, both players' life and pools, and the permissions that decide what
#: can be cast next. All of it is small and none of it scales with the board.
ACTION_ANSWER_SECTIONS: tuple[str, ...] = (
    "stack",
    "combat",
    "players",
    "play_permissions",
    "pregame",
)

#: What an action answer says instead of the board it left out, so a narrowed
#: board is never misread as an empty one - the same promise
#: `battlefield_state`'s own `focus.omitted_sections` makes.
_READ_THE_BOARD_WITH = (
    "battlefield_state - this answer carries the spine, the stack, combat, both players, "
    "and one summary line per permanent under `board.battlefield_digest`. The full "
    "battlefield card views, hands, graveyards, exile and command zone are not missing "
    "from the game, only from this answer; ask for them there, with `sections` to narrow "
    "further."
)


logger = logging.getLogger("mcm.mcp")

#: How many bytes the battlefield digest may take before the WHOLE thing is
#: replaced by a pointer at `battlefield_state`. At the measured ~62 bytes a
#: permanent that is about 128 permanents - so it never fires on a real board,
#: which is the point: this is a bound against a pathological state, not a
#: routine trim. 28 permanents (`test_action_answer_size.py`'s already-busy
#: board) cost 1,726 bytes, a fifth of it.
MAX_DIGEST_BYTES = 8_000

#: What replaces the digest when it will not fit. Wholesale, never partial,
#: and this is the one rule of this block worth stating twice: a seat that
#: misses a blocker because the digest ran out of room is the exact failure
#: this must not have. `omitted_sections` already makes the same promise for
#: the sections it names - say what is missing, never quietly hand over less
#: than was asked for and let it read as all there was.
_DIGEST_TOO_BIG = (
    "battlefield_state - this board has more permanents than one answer can summarise, "
    "so NONE of them are listed here rather than some of them. Ask there for the whole "
    "battlefield; nothing about it is missing from the game, only from this answer."
)


def _digest_line(card: dict[str, Any], attacking: str | None) -> str:
    """One permanent, in one line.

    `id` first because it is what every tool takes back, then the name, then
    only what is actually true of this object: its power and toughness if it
    has any, `T` if it is tapped, `sick` if it could not attack or tap for an
    ability (CR 302.6), each kind of counter it carries, what it is attached
    to, and what it is attacking. A vanilla untapped land is its id and its
    name and nothing else.

    ~62 bytes against the ~1,300 of the card view this is drawn from - the
    oracle text, the image links, the parsed ability list and the printed
    characteristics are all `battlefield_state`'s job and none of them change
    what a seat is choosing between this instant.
    """
    parts = [f"{card.get('id')} {card.get('name')}"]
    power, toughness = card.get("power"), card.get("toughness")
    if power is not None or toughness is not None:
        parts.append(f"{power}/{toughness}")
    if card.get("tapped"):
        parts.append("T")
    if card.get("summoning_sick"):
        parts.append("sick")
    counters = card.get("counters")
    if isinstance(counters, dict):
        parts.extend(f"{kind}x{count}" for kind, count in counters.items() if count)
    attached = card.get("attached_to")
    if isinstance(attached, dict) and attached.get("name"):
        parts.append(f"on {attached['name']}")
    if attacking is not None:
        parts.append(f"attacking {attacking}")
    return " ".join(parts)


def _battlefield_digest(state: dict[str, Any]) -> dict[str, list[str]] | str | None:
    """The battlefield the answer left out, one line per permanent, keyed by
    who controls it - or `_DIGEST_TOO_BIG` when that will not fit.

    Built from the `state` `_action_answer` already receives and currently
    throws away whole, so this costs no fetch and no engine call: it is a
    reshaping of bytes that were already in hand.

    This is emphatically NOT the `battlefield` section, and `omitted_sections`
    goes on saying so. The section is every card's full view; this is a
    summary of it, and calling it the section would be a lie
    `tests/test_action_answer_size.py` correctly catches.

    A player with an empty board gets an empty list rather than no key: an
    empty battlefield is a real, common state, and it is the digest being
    *absent* that a reader could not tell from a digest that was never built.
    """
    battlefield = state.get("battlefield")
    if not isinstance(battlefield, list):
        return None
    attacks = (state.get("combat") or {}).get("attacks") or []
    defender_of = {
        attack.get("attacker"): attack.get("defender")
        for attack in attacks
        if isinstance(attack, dict)
    }
    players = state.get("players")
    digest: dict[str, list[str]] = (
        {
            player["username"]: []
            for player in players
            if isinstance(player, dict) and player.get("username")
        }
        if isinstance(players, list)
        else {}
    )
    for card in battlefield:
        if not isinstance(card, dict):
            continue
        controller = card.get("controller") or card.get("owner") or "?"
        digest.setdefault(controller, []).append(
            _digest_line(card, defender_of.get(card.get("id")))
        )
    if len(json.dumps(digest, indent=1)) > MAX_DIGEST_BYTES:
        return _DIGEST_TOO_BIG
    return digest


def _without_empty(entry: dict[str, Any]) -> dict[str, Any]:
    """`entry` without the keys carrying nothing to act on.

    Every `ActionView` prints all twenty-five of its fields, and on any one
    entry about twenty of them are `null` or `[]` - `ability_index` on a cast,
    `spell_face` on a pass, `target_slots` on a land drop. Nineteen legal
    actions cost 11,440 bytes that way, roughly 600 each, almost all of it
    padding a reader has to skip past. An absent key and a null one say the
    same thing to whoever reads this, and only one of them costs tokens.

    `False` and `0` are kept: both are answers ("this is not modal", "X is
    zero"), and `in` compares by equality, which is why they are not caught by
    the `None`/`[]`/`{}`/`""` test above them.
    """
    return {key: value for key, value in entry.items() if value not in (None, [], {}, "")}


#: How many bytes of target candidates one whole answer may carry, every
#: action entry together. Spent in list order (`_spend_candidate_budget`),
#: and applied here rather than server-side because here is where the token
#: budget actually lives: the api answers one caller with 45,000 bytes to
#: spend and another - the browser - with a whole screen and no budget at all.
#:
#: 12,000 is chosen off the measurement, not off a round number. Fourteen
#: candidates - Lightning Bolt's "any target" on a 28-permanent board - cost
#: 972 bytes as strings, so this is room for roughly a dozen fully-enumerated
#: targeted spells in one hand, which is more targeted spells than a hand
#: holds. An entry that does not fit keeps its `slots[].count` and its
#: `more_with` and loses only the names: the seat still knows the shape of
#: what it could do and where to look, which is the difference between a
#: budget and a silent truncation.
MAX_CANDIDATE_BYTES = 12_000

#: What an entry says when the budget above ran out before it. Distinct from
#: `affordances.MORE_CANDIDATES_WITH` on purpose - "there are more than I
#: listed" and "I listed none of them, for reasons that have nothing to do
#: with your board" are different facts, and a seat reading the second one
#: needs to know its own `count` is still exact.
_BUDGET_SPENT_WITH = (
    "battlefield_state - this answer's candidate budget was spent on the actions above, "
    "so this one lists none of its own. `count` is still the true total, and every "
    "candidate is a permanent, a graveyard/exile card, a spell on the stack, or a player"
)


def _spend_candidate_budget(actions: list[Any]) -> list[Any]:
    """`actions`, with candidate lists kept while `MAX_CANDIDATE_BYTES` lasts
    and dropped once it does not.

    In list order, deliberately: the engine's own enumeration order puts a
    seat's own cast and activation options where it put them, and spending
    the budget in that order means the entries a reader meets first are the
    ones that still have their names. The alternative - trimming every entry
    a little - is the silent partial answer this whole shape exists to avoid.

    Nothing is mutated in place. The entries here are the ones
    `seatkit.available_actions` handed over, and `run_action`'s own return
    (which `seat_cli.py` prints in full) is built from the same objects; a
    budget applied at the MCP boundary must not reach back through them.
    """
    spent = 0
    budgeted: list[Any] = []
    for action in actions:
        targets = action.get("targets") if isinstance(action, dict) else None
        slots = targets.get("slots") if isinstance(targets, dict) else None
        if not isinstance(slots, list):
            budgeted.append(action)
            continue
        cost = len(json.dumps([slot.get("candidates") or [] for slot in slots], indent=1))
        if spent + cost <= MAX_CANDIDATE_BYTES:
            spent += cost
            budgeted.append(action)
            continue
        stripped = {key: value for key, value in targets.items() if key != "more_with"}
        stripped["slots"] = [
            {key: value for key, value in slot.items() if key != "candidates"} for slot in slots
        ]
        stripped["more_with"] = _BUDGET_SPENT_WITH
        budgeted.append({**action, "targets": stripped})
    return budgeted


#: How many bytes of `pays_with` one whole answer may carry with the tapped
#: source's own `name` still attached, every action entry together - spent
#: in list order like `MAX_CANDIDATE_BYTES` above, and for the same reason:
#: `pays_with` was unbudgeted (nothing in this module capped it, or even
#: acknowledged it existed) until an answer-size audit caught it.
#:
#: Measured, not guessed: `test_action_answer_size.py`'s busy board (28
#: permanents, a five-card hand) casting Solitude left three other hand
#: cards affordable, one mana source apiece, and their `pays_with` cost 373
#: bytes for the three entries together - about 124 bytes each, almost all
#: of it the source's own name. 4,000 is room for roughly thirty such
#: entries - a hand of ten multi-source casts - well past anything a real
#: hand holds, while still bounding an adversarial one.
MAX_PAYS_WITH_BYTES = 4_000

#: What a `pays_with` entry's `name` becomes once the budget above is spent.
#: `Action.pays_with` is purely informational (its own docstring: nothing
#: reads one of these back in, and no entry point takes one), so losing the
#: label costs a seat one `battlefield_state`/`card_id` lookup - never the
#: plan itself. `card_id`, `ability_index`, `adds` and `restriction` all
#: survive untouched, which is the difference between this and dropping the
#: field outright: an entry trimmed this way is still a complete, honest
#: statement of what would be tapped and why, only less convenient to read
#: without a second call.
_PAYS_WITH_NAME_OMITTED = (
    "name omitted - this answer's pays_with budget was spent; look this card_id "
    "up with battlefield_state"
)


def _spend_pays_with_budget(actions: list[Any]) -> list[Any]:
    """`actions`, with each `pays_with` entry's own `name` kept while
    `MAX_PAYS_WITH_BYTES` lasts and replaced with `_PAYS_WITH_NAME_OMITTED`
    once it does not.

    In list order, deliberately, the same reasoning `_spend_candidate_budget`
    already carries: the entries a reader meets first keep their names.
    Nothing is mutated in place - a new dict replaces `pays_with` only on
    the entries beyond the budget, the same non-destructive shape
    `_spend_candidate_budget` uses.
    """
    spent = 0
    budgeted: list[Any] = []
    for action in actions:
        pays_with = action.get("pays_with") if isinstance(action, dict) else None
        if not isinstance(pays_with, list) or not pays_with:
            budgeted.append(action)
            continue
        cost = len(json.dumps(pays_with, indent=1))
        if spent + cost <= MAX_PAYS_WITH_BYTES:
            spent += cost
            budgeted.append(action)
            continue
        trimmed = [
            {**entry, "name": _PAYS_WITH_NAME_OMITTED} if isinstance(entry, dict) else entry
            for entry in pays_with
        ]
        budgeted.append({**action, "pays_with": trimmed})
    return budgeted


def _compact_actions(available: Any) -> Any:
    """`available_actions` with each action entry stripped to what it says,
    its target candidates trimmed to what the answer can afford, and its
    `pays_with` entries trimmed to what a separate, smaller budget affords."""
    if not isinstance(available, dict):
        return available
    compacted = _without_empty(available)
    actions = compacted.get("actions")
    if isinstance(actions, list):
        compacted["actions"] = _spend_candidate_budget(
            _spend_pays_with_budget(
                [
                    _without_empty(action) if isinstance(action, dict) else action
                    for action in actions
                ]
            )
        )
    return compacted


def _action_answer(payload: dict[str, Any]) -> dict[str, Any]:
    """One `run_action`/`pass_until` result, reshaped for a reader with a
    token budget: the outcome first, the board's bulk gone, and the keys in
    the order a seat needs them rather than in alphabetical order.

    `seatkit.run_action` itself is deliberately left alone. `seat_cli.py` is
    the other front end onto the same function and is a human at a terminal
    who can scroll; `tests/test_action_flow.py` pins that dict's contract; and
    the narrowing only ever needs to happen where the tokens are spent, which
    is here.

    `pending_decision` is lifted out of the board and up to the top level.
    That is where `_tool_result` looks for it when a payload has no `"state"`
    key (its own docstring), so promoting it keeps the `input_required`
    shaping working with no second check - and it puts the question a seat has
    to answer next to the outcome that raised it, rather than inside a board
    block. It is written only when there actually is one; every other key the
    caller sent is carried through untouched, in the order it arrived, so a
    `pass_until` run keeps `finished`/`stopped_because`/`until`/`resume` and an
    action keeps `held_priority`/`in_flight_action`/`pass_outcome`.

    `outcome` and `wake_reason` lead when the payload has them, `None`
    included: `run_action` sets `outcome` to `None` on purpose when the action
    paused on a decision it could not see through, and dropping that key would
    turn "it has not finished yet" into "nobody said". `pass_until` carries
    neither, and no null placeholder is invented for it.

    `board.battlefield_digest` is the one thing this adds rather than removes
    (`_battlefield_digest`): one line per permanent, built out of the `state`
    this function already receives and used to discard whole. The board is
    what a seat needs to *choose* its next action, and re-serialising every
    card view to give it is what made this answer 25,397 tokens; ~62 bytes a
    permanent gives the same choice for a two-hundredth of the cost.
    `battlefield` stays in `omitted_sections` regardless - a summary of a
    section is not the section.
    """
    state = payload.get("state")
    state = state if isinstance(state, dict) else {}
    board = _select_sections(state, list(ACTION_ANSWER_SECTIONS))
    board.pop("pending_decision", None)
    digest = _battlefield_digest(state)
    if digest is not None:
        board["battlefield_digest"] = digest
    board["omitted_sections"] = sorted(
        section
        for section in SECTION_KEYS
        if section not in ACTION_ANSWER_SECTIONS and section != "pending_decision"
    )
    board["read_the_board_with"] = _READ_THE_BOARD_WITH

    answer: dict[str, Any] = {
        key: payload[key] for key in ("outcome", "wake_reason") if key in payload
    }
    pending = state.get("pending_decision")
    if pending:
        answer["pending_decision"] = pending
    carried = ("outcome", "wake_reason", "state", "available_actions", "recent_log")
    answer.update({key: value for key, value in payload.items() if key not in carried})
    answer["board"] = board
    # Every action tool's answer always carries both keys, null when the
    # payload had none. A script run's answer alone leaves them out: it is its
    # `script_exit` and the board, and a null list there would read as
    # "nothing is legal" rather than "this answer does not say".
    if "script_exit" not in payload:
        answer["available_actions"] = _compact_actions(payload.get("available_actions"))
        answer["recent_log"] = payload.get("recent_log")
    return answer


def build_server(seats: Seats) -> MCPServer:
    server = MCPServer(name="mtg")

    def resolve(player_uid: str) -> Seat | None:
        return seats.seat_for(player_uid)

    def _attach_script_hint(answer: dict[str, Any], seat: Seat, state: dict[str, Any]) -> None:
        """S1: bolt `scripts: {"applicable": [...]}` onto `answer` when at
        least one of `seat`'s registered play scripts applies right now
        (`script_tools.applicable_hint`) - the same treatment `wake_reason`/
        `recent_log` already get on a bare state view, over `state` this
        answer already carries rather than a second board fetch.

        S1 review finding 2: an HTTP error, or anything
        `conditions.evaluate`/`static_conflicts`/`lint` can raise, must never
        replace or break an answer whose action already landed - it is
        caught here, logged once, and `scripts` is simply left off."""
        try:
            hint = script_tools.applicable_hint(seat, seats.script_corpora, state)
        except Exception:
            logger.warning(
                "computing the script hint for %s raised - answer sent without `scripts`",
                seat.uid,
                exc_info=True,
            )
            return
        if hint is not None:
            answer["scripts"] = hint

    def _attach_draft(answer: dict[str, Any], seat: Seat, state: dict[str, Any]) -> None:
        """S2 A6: bolt `draft: {"status": ...}` onto `answer` for an advisor
        with a staged draft (`seatkit.draft_status_for_answer`) - checked on
        every advisor answer that reaches `_tool_result`, action tools and
        `wait_for_priority`/`pass_until` alike, which is what lets a draft get
        posted the moment the human's own poll notices the window (within one
        `wait_for_priority` call's own internal poll, not only on the next
        separate one) rather than only when the buddy happens to call
        something else afterward. `state` carries `armed` whenever this same
        call also fired an armed script (`wait_for_priority`'s own splice),
        which is what lets `draft_status_for_answer` catch the two colliding
        in one answer (review fix 4) before either ever reaches `resolve_
        advice_draft`.

        `None` for a seat, or an advisor with nothing staged - `draft` is left
        off the answer entirely rather than invented as some idle status.

        `answer.get("script_exit")` (review fix 2, 2026-09-14) is threaded
        through too - a MANUAL `run_script` call's own `script_exit` is this
        very answer's own proposal, exactly as `state["armed"]` is for an
        armed script `wait_for_priority` fires inside its own poll. Every
        other tool's `answer` simply has no `script_exit` key, so this is a
        no-op for them.

        Same failure discipline as `_attach_script_hint`: an exception here
        must never break the tool answer whose action already landed, so it
        is caught, logged once, and `draft` is simply left off."""
        try:
            result = seatkit.draft_status_for_answer(
                seat, state, script_exit=answer.get("script_exit")
            )
        except Exception:
            logger.warning(
                "resolving %s's staged advice draft raised - answer sent without `draft`",
                seat.uid,
                exc_info=True,
            )
            return
        if result is not None:
            answer["draft"] = result

    def _tool_result(
        seat: Seat, payload: dict[str, Any], protocol_version: str | None
    ) -> str | InputRequiredResult:
        """Shape one tool's JSON answer, surfacing a decision pending for
        this very seat as an MCP 2026-07-28 `input_required` result
        (`InputRequiredResult`, `result_type: "input_required"` on the wire)
        instead of plain text - but only when the connected client actually
        negotiated a revision that knows that shape.

        `payload` is either a whole state view (`wait_for_priority`) or a
        `run_action`/`pass_until` result carrying one under `"state"` (every
        action tool, via `act`). Which of the two it is decides both the
        shaping and where the pending decision is read from, and the presence
        of a `"state"` key is what tells them apart - no caller has to say.

        A state view is passed through as it always was: `wait_for_priority`
        and `battlefield_state` exist to hand a seat the board, and narrowing
        their answer would be narrowing the thing that was asked for.

        An action's answer goes through `_action_answer` first, which drops
        the board's bulk (the battlefield, both hands, both graveyards, exile,
        the command zone - all of it `battlefield_state`'s job), lifts
        `pending_decision` to the top level, and writes `outcome` first. See
        that function for the 25,397-token `cast` answer this exists to stop
        being produced.

        Only a decision addressed to *this* seat gets the treatment: one
        pending for the opponent is not this call's business to interrupt
        for, and comes back as ordinary text the same as "nothing new yet".

        **`protocol_version` decides whether that treatment is even legal to
        offer.** `InputRequiredResult`'s wire shape carries no `content`
        field, which is fine against MCP 2026-07-28's own result surface
        (`MODERN_PROTOCOL_VERSIONS`) but fails validation on every earlier
        revision, whose `CallToolResult` requires one - `MCPServer` does NOT
        dispatch a result shape by a tool's declared return type (there is no
        such mechanism; the framework's own version gate,
        `_uses_input_required`, guards only its built-in elicitation path,
        which this server does not use). A hand-rolled `InputRequiredResult`
        like this one gets no version guard from the framework at all, so
        this function is where that guard has to live - the real runner
        validates whatever comes back here against the negotiated version's
        own result surface (`mcp_types.methods.serialize_server_result`)
        before it ever reaches the wire, and raises "Handler returned an
        invalid result" if it does not fit. `protocol_version=None` (no real
        request in flight - a test calling a tool directly, say) reads as
        modern rather than legacy: every real request carries a negotiated
        version by construction, so `None` only ever happens off that path,
        and defaulting it to the safer-for-old-clients-but-wrong reading
        would make the common, exercised case silently regress instead of
        the one that structurally cannot occur in production. The pending
        decision itself is never dropped either way - a legacy client still
        gets it, as ordinary text, just without the distinct wire shape."""
        if "state" in payload:
            # An action's answer, not a board: narrow it and put the outcome
            # first (`_action_answer`). `sort_keys` is off for this shape on
            # purpose - alphabetical order buried the 84-byte `outcome` at
            # character 13,724, behind the whole legal-action list, in the
            # very answer a stuck seat was trying to read the refusal out of.
            answer = _action_answer(payload)
            raw_state = payload.get("state")
            state_view = raw_state if isinstance(raw_state, dict) else {}
            _attach_script_hint(answer, seat, state_view)
            _attach_draft(answer, seat, state_view)
            pending = answer.get("pending_decision")
            text = json.dumps(answer, indent=1)
        else:
            _attach_script_hint(payload, seat, payload)
            _attach_draft(payload, seat, payload)
            pending = payload.get("pending_decision")
            text = json.dumps(payload, indent=1, sort_keys=True)
        modern = protocol_version is None or protocol_version in MODERN_PROTOCOL_VERSIONS
        if isinstance(pending, dict) and pending.get("player") == seat.player and modern:
            return InputRequiredResult(request_state=text)
        return text

    def act(
        seat: Seat,
        perform: Any,
        hold_priority: bool,
        release: bool = True,
        reasoning: str = "",
        *,
        protocol_version: str | None,
    ) -> str | InputRequiredResult:
        result = run_action(
            seat, perform, hold_priority=hold_priority, release=release, reasoning=reasoning or None
        )
        return _tool_result(seat, result, protocol_version)

    @server.tool()
    @_guarded(seats)
    def wait_for_priority(
        player_uid: str,
        max_seconds: float = 0.0,
        arm: str | list[str] = "",
        arm_reasoning: str = "",
        opposing_archetype: str = "*",
        inputs: dict[str, Any] | None = None,
        wake_to_draft: bool = False,
        ctx: Context | None = None,
    ) -> str | InputRequiredResult:
        """Check whether it is your moment to act, then hand you the board.

                The check looks for exactly one of these, right now:
                - a decision is waiting for you (CR 608.2d - use decide() to answer;
                  the answer itself arrives shaped `input_required` rather than
                  plain text, so an MCP client that understands the 2026-07-28
                  surface can tell at a glance that this call cannot go further
                  until you resolve it),
                - you have priority (act, or pass),
                - the game is over (a winner is set, or it is a draw),
                - the step gives nobody priority (untap, cleanup) and you are
                  the active player. Nothing is asked of you there (CR 502.4,
                  CR 514.3) and the step ends of its own accord (CR 500.3), so
                  the game will already have moved on by the time you look
                  again.
                - the pregame is waiting on **you**: your own CR 103.5 mulligan
                  declaration, or your own CR 103.6 opening-hand actions.

                Waiting on somebody *else's* pregame declaration is expressly
                not one of them. CR 103.5 has each player declare in turn order
                and nothing happens until every player has ("Once each player
                has made a declaration, all players who decided to take
                mulligans do so at the same time"), so a seat that has already
                declared has no move to make - `max_seconds` applies there like
                anywhere else. It did not always: this used to report "another
                seat owes a pregame declaration" as a wake, which returned
                before the bound could bind, and a live seat pod spent 53
                consecutive calls and one inference every 3.2 seconds
                re-reading a board that had not changed.

        **`max_seconds`: use this instead of `Bash: sleep` between calls.**
                Left at its default `0.0`, this checks once and returns at once,
                exactly as it always has. Passed a positive value (capped at
                `seatkit.MAX_WAIT_SECONDS`), and if none of the above holds yet, it
                keeps checking on the server for up to that long before giving up -
                still always returns on its own, still never holds the connection
                open past the bound (MCP 2026-07-28 removed resumability, SEP-2577
                - the reason the *unbounded* version of this wait was removed
                outright rather than merely shortened; this is the bounded
                replacement, not that wait brought back). Ask for a wait here
                rather than sleeping in `Bash` and calling again: that is a whole
                extra tool call and turn spent on exactly what this one parameter
                already does for you.

                When nothing is new (whether you asked for `max_seconds` or not),
                the state's own `wake_reason` says so plainly (`nothing new yet -
                call again to check`). Every other action tool already checks the
                same way right after it runs, so you normally do not need this
                separately - it exists for "I have done nothing yet, is it my
                turn", and for the wait itself when the answer is "not yet".

        **A game that is gone ends the wait at once.** Once the game has been
                released or deleted, `wake_reason` says so (`the game is gone:
                ...`): stop calling the game tools for it. A call cancelled
                while it waits returns at once with `the wait was interrupted`.

        **An advisor uid also gets `available_actions` on a real wake** - the
                same compacted list, target `candidates` and `pays_with`
                included, that a seat gets on `pass`/`pass_until`. An advisor is
                refused `pass_until`, so this is the answer that wakes it. It
                is fetched only when a wake condition holds: an answer saying
                `nothing new yet` carries no list. A seat's own wait carries
                none either - its list rides the actions it takes.

        **`arm` (S2)**: a list of registered script ids (or a comma-separated
                string of them), in priority order, to fire on their own the moment this wait wakes
                for a real reason - "wait until you could require picking scripts
                to run: if the situation still matches a script's conditions, it
                fires automatically". Never mandatory: left empty (the default),
                this behaves exactly as it always has. Judged the same way
                `run_script`/the hint judge; two armed scripts applying at once
                is a conflict and neither fires (`armed_conflict` names them).
                `arm_reasoning` is required whenever `arm` is non-empty - every
                fire is judged and recorded with it. `opposing_archetype` and
                `inputs` mean what they mean for `run_script`. An advisor here
                fires in advise mode: the first applicable armed script proposes
                a move (`advised`) and the batch stops there.

        **`wake_to_draft` (S2 A6, advisor only)**: also wake when the board
                changes while it is not yet your human's priority, provided you
                have no draft staged right now - the cue to look ahead and
                `stage_advice` for the window you expect next, so a proposal is
                ready to post the instant priority actually reaches them, rather
                than drafted from scratch only once it does. Left false (the
                default), this behaves exactly as it always has. Refused outright
                for a seat: a seat already wakes for its own real moments, and
                there is no draft for it to prepare.

                Every answer here, and every action tool's, also carries `draft`
                once you have one staged: `{"status": "held"}` while it is not yet
                your human's turn at that window, `{"status": "posted", "log_seq":
                ...}` the moment it is and everything still holds up, or
                `{"status": "stale", "failed": "window"|"board"|"legality"}` the
                moment any one of those three does not - the board changed
                underneath it, or the window itself has already passed. A stale
                draft is dropped; this wait wakes you to draft another the next
                time the board moves and you asked for `wake_to_draft`.
        """
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        if wake_to_draft and not seat.advise:
            return seatkit.WAKE_TO_DRAFT_ADVISOR_ONLY
        staged = seats.advice_drafts.get(seat.uid)
        draft_staged = staged is not None and staged.get("action") is not None
        woken = seatkit.wait_and_arm(
            seat,
            before=None,
            max_seconds=max_seconds,
            should_stop=cancel_probe(ctx),
            corpora=seats.script_corpora,
            arm=_parse_ids(arm),
            arm_reasoning=arm_reasoning,
            opposing_archetype=opposing_archetype,
            inputs=inputs,
            wake_to_draft=wake_to_draft and seat.advise,
            draft_staged=draft_staged,
        )
        woken["state"]["wake_reason"] = woken["wake_reason"]
        woken["state"]["recent_log"] = woken["recent_log"]
        if "armed" in woken:
            woken["state"]["armed"] = woken["armed"]
        if "armed_conflict" in woken:
            woken["state"]["armed_conflict"] = woken["armed_conflict"]
        if "armed_refusal" in woken:
            woken["state"]["armed_refusal"] = woken["armed_refusal"]
        if seat.advise and woken["wake_reason"] != seatkit.NOTHING_NEW_YET:
            # The advisor's legal plays, on the one answer that wakes it
            # (`pass_until` is refused to an advise seat). A GET, like every
            # other read here, and only on a real wake: an idle poll would pay
            # ~106 ms of server time for a list nobody can use. Shaped by the
            # action answers' own `_compact_actions`, so it spends the same
            # candidate and `pays_with` budgets and says so when it runs out.
            woken["state"]["available_actions"] = _compact_actions(
                seatkit.available_actions(seat, woken["state"])
            )
        return _tool_result(seat, woken["state"], ctx.protocol_version if ctx is not None else None)

    @server.tool()
    @_guarded(seats)
    def battlefield_state(player_uid: str, sections: str = "", side: str = "both") -> str:
        """The game as you may see it: your hand, both sides of the public
        board (battlefield, stack, graveyards, exile), life and mana pools,
        turn/step, priority, and combat. The opponent's hand and both
        libraries are hidden - only their sizes are shown.

        **Call it with nothing but your uid and you get all of that**, which
        is what you normally want. The two options below narrow the answer
        when you already know which part of the board your next question is
        about - they can only ever leave things out, never add anything you
        could not otherwise see.

        `sections` is a comma-separated list of the parts to keep, named with
        this answer's own top-level keys, so what you ask for is spelled like
        what you read: `battlefield`, `hands`, `graveyards`, `exile` (which
        brings `exile_pile` with it), `command`, `stack`, `combat`,
        `players`, `play_permissions`, `pregame`, `pending_decision`,
        `recent_log`, `known_cards`, `library_contents`, `sideboard_contents`.
        An empty `sections` - the default - is all of them except the last
        two, which come only when you name them. A name that is not on that
        list is refused, with the list, rather than quietly ignored.

        `known_cards` is what you know about hidden cards because an effect
        showed them to you or tracked where they went: `hand` lists cards in
        another player's hand that were revealed to you or that you looked at
        (and still sit there), `library` lists library cards you know with
        `index_from_top` (their position right now - a shuffle ends it; a card
        put into a library from a public zone is known to every player), and
        `sideboard` lists sideboard cards revealed to you. Your own hand and
        sideboard are not repeated there; a look at your own library is.

        `library_contents` and `sideboard_contents` are your own library and
        sideboard as `{card, count}` lists sorted by name - what is in them,
        never in what order, and never the opponent's.

        `side` is `both` (the default), `mine`, or `opponent`, and it is
        resolved from the uid you passed, not from a username you have to get
        right: `mine` is your own seat and `opponent` is every other seat.
        That matters in a mirror match, where both boards hold the same card
        names - the answer's `focus` block says which usernames each side
        resolved to (`you`, `opponents`), so you never have to infer it. It
        narrows every section whose entries say whose they are - the
        battlefield, hands, graveyards, exile, the command zone, the stack,
        `players`, `play_permissions`, the contents lists, and the records
        inside `known_cards` (by whose card each one is). It does NOT narrow `combat` (an
        attack belongs to both sides at once), `pregame`, `pending_decision`
        or `recent_log`.

        Whatever you narrow to, the answer always carries the game id, the
        turn, the step, the phase, the active player, the priority holder,
        the result if the game is over, and `you`. `focus` also lists
        `omitted_sections`, so a narrowed board can never be misread as an
        empty one: if you did not ask for `battlefield`, the absence of a
        battlefield here is your own doing, not an empty board.

        `pending_decision` is the question you are being asked right now,
        answered with `decide` - see that tool for the field each `kind`
        wants. Two of its fields are easy to miss and matter most on a
        look-then-split (`scry`/`surveil`): `moved_label` and `kept_label`
        name where each half of your answer actually goes, in the asking
        card's own words ("top of your library", "exiled", "your graveyard",
        "bottom of your library"). They are authoritative whenever they are
        non-empty, and the `scry`/`surveil` kind alone is not - the engine
        reuses that kind for any look-then-split, including Devourer of
        Destiny's, which keeps the named card on top and exiles the rest.

        Three more of its fields carry a `choose_cards` - a discard, and
        every library search (CR 701.23a's find, asked as one choice over
        every legal candidate at once). `exact` says whether your answer
        must name exactly `amount` cards (CR 701.23d's quantity search) or
        may name any number from zero up to it (CR 701.23b's declinable
        search, where finding nothing is a real answer). `card_labels` names
        each `cards` entry positionally, and is what you read a hidden-zone
        candidate by - your own library is stripped from this state.
        `default_cards`, when non-null, is what the engine takes if you
        never answer.

        `recent_log` is the other half of the same question, and the reason
        you do not need a second call to ask it: the tail of what has
        actually happened - both seats, allowed and refused, in the game's
        own words - so "what happened since I last acted" and "what does the
        board look like now" cost one round trip between them rather than
        two. Its `omitted` counts what the tail dropped, and `game_log` is
        still there for the whole history and for the full board snapshot
        each entry carries."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        asked = [name.strip() for name in sections.split(",") if name.strip()]
        unknown = [name for name in asked if name not in SECTION_KEYS]
        if unknown:
            return (
                f"no such section: {', '.join(repr(name) for name in unknown)}. "
                f"battlefield_state takes any comma-separated mix of "
                f"{', '.join(sorted(SECTION_KEYS))} - or no sections at all, "
                "which is the whole board."
            )
        if side not in _SIDES:
            return (
                f"no such side: {side!r}. battlefield_state takes side="
                f"{', '.join(_SIDES)} - resolved from your own uid, never a "
                "username. Nothing was returned; ask again with one of those."
            )
        wanted = sorted(asked) if asked else sorted(set(SECTION_KEYS) - set(ON_REQUEST_SECTIONS))
        state = seat.fetch_state(
            include_contents=any(section in ON_REQUEST_SECTIONS for section in wanted)
        )
        # Only fetched when it was asked for: `recent_log` is its own HTTP
        # call to the API (`Seat.recent_log`), so a seat that narrowed it away
        # saves the round trip rather than merely hiding its answer.
        if "recent_log" in wanted:
            state["recent_log"] = seat.recent_log()
        seated = [
            player.get("username")
            for player in state.get("players", [])
            if isinstance(player, dict)
        ]
        opponents = [name for name in seated if name != seat.player and name is not None]
        allowed = {
            "both": None,
            "mine": {seat.player},
            "opponent": set(opponents),
        }[side]
        view = _select_sections(_narrow_to_side(state, allowed), wanted)
        view["focus"] = {
            "sections": wanted,
            "omitted_sections": sorted(set(SECTION_KEYS) - set(wanted)),
            "side": side,
            "you": seat.player,
            "opponents": opponents,
        }
        return json.dumps(view, indent=1, sort_keys=True)

    @server.tool()
    @_guarded(seats)
    def cast(
        player_uid: str,
        card: str,
        spell_face: str = "",
        targets: str = "",
        mana_sources: str = "",
        modes: str = "",
        x_value: int | None = None,
        keyword: str = "",
        alternative_cost: str = "",
        choosing: str = "",
        sacrificing: str = "",
        discarding: str = "",
        exiling: str = "",
        tapping: str = "",
        delve: str = "",
        convoke: str = "",
        improvise: str = "",
        optional_costs: str = "",
        dry_run: bool = False,
        hold_priority: bool = False,
        reasoning: str = "",
        ctx: Context | None = None,
    ) -> str | InputRequiredResult:
        """Cast a spell. Most casts are from your hand, and need nothing
                else; `card` is the card's name or id. `targets` is comma-separated
                card ids, player names, or ids of abilities on the stack.
                `mana_sources` is comma-separated source
                ids, and only the sources named are sent; leave it empty to name
                none, and the engine's automatic payment taps only what the cost
                needs from your untapped permanents. Set `dry_run` to true to ask
                "could I cast this?" without changing anything (a dry run never
                blocks).

        **A split card is two casts, and `spell_face` is how you say which.**
                Wear // Tear is offered to you as TWO `cast` entries in
                `available_actions`, one per half, each carrying its own
                `spell_face` and its own cost - because CR 709.3 makes the half a
                choice you announce "before putting it onto the stack", and
                CR 709.3b gives the spell on the stack "only the characteristics of
                the half being cast". Pass back the `spell_face` string from the row
                you picked, exactly as you read it (`spell_face="Tear"`): it is the
                engine's own spelling of that half's name and nothing here
                transforms it. Leave it empty for the front half, which is every
                ordinary, single-faced card. Tear takes an enchantment and costs
                {W} where Wear takes an artifact and costs {1}{R}, so sending the
                wrong one is refused on the target rather than silently cast.

                An adventurer card (CR 715) works the same way - the creature as a
                faceless row, its Adventure as a `spell_face` one. Take the
                Adventure by sending its row's `spell_face`
                (`spell_face="Swift End"`): CR 715.3a evaluates only the
                Adventure's own characteristics, so it costs what the Adventure
                costs, targets what the Adventure targets, and CR 715.3d exiles
                the card as it resolves rather than putting it in your graveyard -
                from where you may later cast the creature half.

                A card sitting in your graveyard or exile is castable too, but only
                under a *named* permission (CR 601.3: "a player can begin to cast a
                spell only if a rule or effect allows that player to cast it"), and
                `keyword` is how you name it. A card in your **hand** takes the same
                `keyword` for a different reason: CR 118.9's alternative cost, paid
                instead of the printed mana cost - evoking Wistfulness for
                {G/U}{G/U} rather than {3}{G/U}{G/U} is `keyword="Evoke"`, and
                nothing else about the call changes (the option's own `cost` says
                what you would pay, and it is not always mana - Grief's evoke cost
                exiles a black card from your hand).
                Read your `battlefield_state`'s card in whichever zone it is -
                `cast_options` names every keyword that card currently offers,
                alongside the cost it would pay - rather than guessing; the keywords
                this engine knows are:

                - `"Flashback"` - a GRAVEYARD card with flashback (CR 702.34a).
                - `"Mayhem"` - a GRAVEYARD card with mayhem, discarded this turn
                  (CR 702.187a/b).
                - `"Plot"` - an EXILE card you plotted on an earlier turn
                  (CR 702.170d) - `exile_pile` on the card names which.
                - `"Foretell"` - an EXILE card you foretold on an earlier turn
                  (CR 702.143a).
                - `"Madness"` - an EXILE card put there by its own madness trigger
                  this turn (CR 702.35a/b).
                - `"Airbend"` - an EXILE card an airbend effect exiled, for {2}
                  (CR 701.65a).
                - `"Evoke"` (CR 702.74a), `"Dash"` (CR 702.109a), `"Morph"`
                  (CR 702.37a), `"Megamorph"` (CR 702.37b), `"Disguise"`
                  (CR 702.168a), `"Bestow"` (CR 702.103a), `"Warp"` (CR 702.185a) -
                  a HAND card, cast for that keyword's cost rather than its printed
                  mana cost. You do not need one of these to cast from hand; it is
                  offered when the cheaper cost is what you want, and the card's own
                  `cast_options` entry says whether it is available right now.
                - a printed sentence opening one of the above from an extra zone
                  (CR 604.6/CR 113.6f), or a keyword another permanent's static
                  ability is granting the spell right now (CR 610.5, CR 611.3d) -
                  `keyword` is still that keyword's own name either way.

                A card covered by a live standing `PlayPermission` needs no `keyword`
                either - Past in Flames' graveyard grant, a flashback grant, a card
                exiled and made playable "this turn" - and `play_permissions` in your
                state names every one that is currently live for you.

                Cascade's hit and a suspended card's own free cast are NOT
                `PlayPermission`s (battle 8): accepting either casts the card
                immediately while the permitting trigger is still resolving (CR
                608.2g), through the SAME `decide` call that accepted the "you may
                cast it" - answer whichever face, mode, target or additional cost
                (kicker, entwine, escalate, ...) it then asks, one `pending_decision`
                at a time, chained off that accept. There is no later `cast` to make
                for either of these, and nothing shows up in `play_permissions` for
                them at all.

        **A cost is not always mana, and `targets` is never how you pay one.**
                `targets` is the spell's own targets (CR 601.2c) and nothing else -
                naming a card there to pay a cost is refused with "too many targets".
                Every cost that needs you to *name objects* has its own parameter,
                and each takes comma-separated card ids:

                - `choosing` - the objects an ALTERNATIVE cost (CR 118.9) requires
                  you to supply. Solitude's evoke cost is not mana at all: it reads
                  "Exile a white card from your hand" (CR 702.74a), and `choosing`
                  is the white card. So is the black card Grief's exiles, the blue
                  card pitched to Force of Negation, and the Mountains sacrificed
                  to Fireblast. **Announce the cost and name its objects in the
                  same call**: `cast(card="Solitude", keyword="Evoke",
                  choosing="<a white card in your hand>")`. Leaving it out is
                  refused with the engine's own count - "exile needs 1 card(s)
                  chosen, 0 given" - never a silent no-op. `GET
                  .../affordances/{card_id}`'s `alternative_costs[i]` says how many
                  (`requires`) and which cards qualify (`candidates`).
                - `alternative_cost` - the same CR 601.2b announcement named by its
                  `index` (or source name) out of that same `alternative_costs`
                  list, for an alternative cost that has no keyword of its own -
                  Force of Negation's printed "you may exile a blue card from your
                  hand rather than pay this spell's mana cost". `keyword` and
                  `alternative_cost` are two spellings of one announcement; only
                  one may ever be paid (CR 118.9a), and `choosing` feeds whichever
                  you used.
                - `sacrificing`, `discarding`, `exiling`, `tapping` - the objects a
                  printed ADDITIONAL cost asks for (CR 118.8a, "As an additional
                  cost to cast this spell, ..."): Fling's creature, Cathartic
                  Reunion's two cards, Abhorrent Oculus's six graveyard cards,
                  Swallow Whole's untapped creature. Additional, not alternative -
                  the printed mana cost is still paid on top. Name as many ids as
                  the sentence asks for; too few is refused with the same count.
                - `optional_costs` - the OPTIONAL additional costs you choose to
                  pay (CR 601.2b): comma-separated keywords taken from the cast
                  row's own `optional_costs` in `available_actions`.
                  `optional_costs="Kicker"` kicks Burst Lightning for {4} more
                  (CR 702.33a); a repeatable cost takes a count
                  (`"Multikicker:2"`), a gift names the opponent (`"Gift:bob"`),
                  and a cost naming objects takes their ids
                  (`"Blight:<creature id>"`). `"Entwine"` chooses every mode
                  (list them in `modes` too, CR 702.42a); escalate is charged
                  for each mode beyond the first (CR 702.120a), and
                  `"Escalate:<card id>"` names what a non-mana escalate cost
                  discards; a `keyword="Harmonize"` cast taps the creature
                  named by `"Harmonize:<creature id>"` to cut its cost by that
                  creature's power (CR 702.180a). Leave it empty to pay none.
                - `delve` - graveyard cards exiled to pay generic mana
                  (CR 702.66a), one per generic symbol.
                - `convoke` - your untapped creatures tapped to pay (CR 702.51a).
                  A creature pays "{1} or one mana of that creature's color", and
                  which one is your choice, so an entry may be a bare id (the
                  generic symbol) or `<id>:W` to cover a coloured one. Four white
                  creatures offered against `{3}{W}` are three bare ids and one
                  `<id>:W`.
                - `improvise` - your untapped artifacts tapped to pay one generic
                  mana each (CR 702.126a), comma-separated ids. A cast row that is
                  payable this way names them in its own `improvise`.

                `GET .../affordances/{card_id}` is the one place that answers all of
                these for a specific card, and a hand card's own `cast_options` in
                `battlefield_state` already names each keyword and the cost it would
                pay. Read one of those rather than guessing a parameter name.

        Otherwise this does the whole turn of events in one call: cast, then
                - unless `hold_priority` is true - release priority and check once
                for your next moment to act. **No tool in this server ever blocks or
                waits**, this one included (poll-and-return - MCP 2026-07-28 removed
                resumability, `seatkit.block_until_wake`'s own docstring); there is no
                `max_seconds` on any of them, because there is nothing left for one to
                bound. If `wake_reason` says "nothing new yet",
                call this (or `wait_for_priority`) again to keep checking. The
                answer carries the cast's own outcome, the state that check saw, and
                that `wake_reason` - except when the check finds a decision pending
                for you (yours to answer with `decide`, whether this cast itself
                raised it or something else did), in which case the whole answer
                arrives shaped `input_required` instead of plain text (an
                `InputRequiredResult`, `result_type: "input_required"` on the wire) -
                every tool built on this same flow shares that shape, not just this
                one. Set `hold_priority` to cast again, or to respond to your own
                spell, without giving up the chance to act again first (CR 117.3c:
                you already keep priority after casting - this only decides whether
                the harness auto-passes it for you). Releasing is the default here.
                The human play UI's stops panel has its own "Keep priority by
                default" tick, on by default, which is the same override as
                `hold_priority=true` here.

        `reasoning`: why you are casting this, in your own words - required
        for an agent seat (a missing one is refused with a 422 naming the
        field), optional for a human. Every action tool takes it the same way."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body: dict[str, Any] = {"player": seat.player, "dry_run": dry_run}
        body.update(card_reference(card))
        if spell_face.strip():
            # CR 709.3's announcement, carried verbatim: the value a seat read
            # off `Action.spell_face` is the value `CastRequest.spell_face`
            # takes, with only the MCP wire's empty-string default stripped off
            # (every string parameter on this server spells "absent" that way).
            body["spell_face"] = spell_face.strip()
        if targets:
            body["targets"] = _parse_ids(targets)
        sources = _parse_ids(mana_sources)
        if sources:
            body["mana_sources"] = sources
        if modes:
            body["modes"] = [int(mode) for mode in _parse_ids(modes)]
        if x_value is not None:
            body["x_value"] = x_value
        if keyword:
            body["keyword"] = keyword
        if alternative_cost:
            # `CastRequest.alternative_cost` is `int | str`: an index into
            # `alternative_costs`, or the option's own keyword/source name.
            # The MCP wire has one string type for this, so a caller who
            # answered with the index sends its digits - handed on as an int
            # so the request means the index it looks like, not a source
            # named "0".
            body["alternative_cost"] = (
                int(alternative_cost) if alternative_cost.strip().isdigit() else alternative_cost
            )
        for field, value in (
            ("choosing", choosing),
            ("sacrificing", sacrificing),
            ("discarding", discarding),
            ("exiling", exiling),
            ("tapping", tapping),
            ("delve", delve),
            ("improvise", improvise),
        ):
            if value:
                body[field] = _parse_ids(value)
        if convoke:
            body["convoke"] = parse_convoke(convoke)
        if optional_costs:
            body["optional_costs"] = parse_optional_costs(optional_costs)
        if reasoning:
            body["reasoning"] = reasoning
        if dry_run:
            return seat.call("POST", f"/api/games/{seat.game_id()}/actions/cast", body)

        def perform() -> Any:
            return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/cast", body)

        return act(
            seat,
            perform,
            hold_priority,
            reasoning=reasoning,
            protocol_version=ctx.protocol_version if ctx is not None else None,
        )

    def _special_action_tool(action: str) -> Any:
        """suspend/plot/foretell share `cast`'s exact shape, minus targeting
        and modes: locate a hand card, pay its own special-action cost, then
        release-and-wait like every other action tool - see `cast`'s
        docstring for the shared answer shape."""

        def tool(
            player_uid: str,
            card: str,
            mana_sources: str = "",
            dry_run: bool = False,
            hold_priority: bool = False,
            reasoning: str = "",
            ctx: Context | None = None,
        ) -> str | InputRequiredResult:
            seat = resolve(player_uid)
            if seat is None:
                return NO_SUCH_SEAT
            state = seat.fetch_state()
            body: dict[str, Any] = {"player": seat.player, "dry_run": dry_run}
            body.update(card_reference(card))
            sources = seat.sources_or_all(mana_sources, state)
            if sources:
                body["mana_sources"] = sources
            if reasoning:
                body["reasoning"] = reasoning
            if dry_run:
                return seat.call("POST", f"/api/games/{seat.game_id()}/actions/{action}", body)

            def perform() -> Any:
                return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/{action}", body)

            return act(
                seat,
                perform,
                hold_priority,
                reasoning=reasoning,
                protocol_version=ctx.protocol_version if ctx is not None else None,
            )

        return tool

    @server.tool()
    @_guarded(seats)
    def suspend(
        player_uid: str,
        card: str,
        mana_sources: str = "",
        dry_run: bool = False,
        hold_priority: bool = True,
        reasoning: str = "",
        ctx: Context | None = None,
    ) -> str | InputRequiredResult:
        """CR 116.2f/702.62a: exile a card with suspend from your hand, paying
        its suspend cost, with N time counters on it. `card` is the card's
        name or id in your hand. `mana_sources` is comma-separated source ids;
        leave it empty to offer all your untapped mana sources.

        Once N reaches zero (removed one per your own upkeep, CR 702.62a),
        you may cast it without paying its mana cost: call
        `cast` on it with no `keyword`, the permission is picked up
        automatically.

        HOLDS PRIORITY BY DEFAULT, unlike `cast`: suspending is a special
        action (CR 116.2f), and CR 116.3 says "if a player takes a special
        action, that player receives priority afterward". A special action
        does not use the stack (CR 116.1), so there is nothing to release
        priority for - releasing would just end your own main phase with the
        rest of your plan unplayed. Pass `hold_priority=false` only when you
        really do want to hand priority over - which still does not block or
        wait, no branch of this tool does. See `cast`'s docstring for the
        answer's shape."""
        return _special_action_tool("suspend")(
            player_uid, card, mana_sources, dry_run, hold_priority, reasoning, ctx
        )

    @server.tool()
    @_guarded(seats)
    def plot(
        player_uid: str,
        card: str,
        mana_sources: str = "",
        dry_run: bool = False,
        hold_priority: bool = True,
        reasoning: str = "",
        ctx: Context | None = None,
    ) -> str | InputRequiredResult:
        """CR 116.2k/702.170b: exile a card with plot from your hand, paying
        its plot cost. `card` is the card's name or id in your hand.
        `mana_sources` is comma-separated source ids; leave it empty to offer
        all your untapped mana sources.

        On any later turn, cast it from exile without paying its mana cost:
        call `cast` on it with `keyword="Plot"`.

        HOLDS PRIORITY BY DEFAULT, unlike `cast`: plotting is a special action
        (CR 116.2k, CR 702.170b), and CR 116.3 says "if a player takes a
        special action, that player receives priority afterward". A special
        action does not use the stack (CR 116.1), so there is nothing to
        release priority for - releasing would just end your own main phase
        with the rest of your plan unplayed. Pass `hold_priority=false` only
        when you really do want to hand priority over - which still does not
        block or wait, no branch of this tool does. See `cast`'s docstring
        for the answer's shape."""
        return _special_action_tool("plot")(
            player_uid, card, mana_sources, dry_run, hold_priority, reasoning, ctx
        )

    @server.tool()
    @_guarded(seats)
    def foretell(
        player_uid: str,
        card: str,
        mana_sources: str = "",
        dry_run: bool = False,
        hold_priority: bool = True,
        reasoning: str = "",
        ctx: Context | None = None,
    ) -> str | InputRequiredResult:
        """CR 116.2h/702.143b: pay {2} and exile a card with foretell from
        your hand, face down (opponents see only that a face-down card is
        there, not what it is). `card` is the card's name or id in your hand.
        `mana_sources` is comma-separated source ids; leave it empty to offer
        all your untapped mana sources.

        On any later turn, cast it from exile for its printed foretell cost
        (not its mana cost): call `cast` on it with `keyword="Foretell"`.

        HOLDS PRIORITY BY DEFAULT, unlike `cast`: foretelling is a special
        action (CR 116.2h, CR 702.143b), and CR 116.3 says "if a player takes
        a special action, that player receives priority afterward". A special
        action does not use the stack (CR 116.1), so there is nothing to
        release priority for - releasing would just end your own main phase
        with the rest of your plan unplayed. Pass `hold_priority=false` only
        when you really do want to hand priority over - which still does not
        block or wait, no branch of this tool does. See `cast`'s docstring
        for the answer's shape."""
        return _special_action_tool("foretell")(
            player_uid, card, mana_sources, dry_run, hold_priority, reasoning, ctx
        )

    @server.tool()
    @_guarded(seats)
    def cycle(
        player_uid: str,
        card: str,
        ability_index: int = 0,
        mana_sources: str = "",
        dry_run: bool = False,
        hold_priority: bool = True,
        reasoning: str = "",
        ctx: Context | None = None,
    ) -> str | InputRequiredResult:
        """CR 702.29a: discard a card with cycling from your hand, paying its
        cycling cost, to draw a card (or search your library for a card of the
        named type, for a typecycling ability). `card` is the card's name or
        id in your hand. `ability_index` picks which cycling ability, for the
        rare card printing more than one line ending "cycling" - almost
        always 0. `mana_sources` is comma-separated source ids; leave it
        empty to offer all your untapped mana sources.

        This is ONLY for a card whose state view names an actual cycling
        ability. A card that discards itself as an activated ability's cost
        without being cycling - Waker of Waves' plain `{1}{U}, Discard this
        card: ...`, Boseiju's or Otawara's Channel - is CR 113.6j's more
        general shape, not this one, and belongs to `activate` instead. If
        this tool refuses with "has no cycling ability number N", that is the
        engine telling you the ability you want is not cycling at all - check
        `activate` before assuming the action does not exist.

        Unlike a mana ability, cycling uses the stack (it cannot add mana, so
        CR 605.1a's definition never catches it) and can be responded to - it
        does not resolve as part of this call.

        HOLDS PRIORITY BY DEFAULT, unlike `cast`. Note the warrant is NOT the
        one `suspend`/`plot`/`foretell` cite: cycling is not a special action
        at all, CR 702.29a makes it an ordinary activated ability
        ("'Cycling [cost]' means '[Cost], Discard this card: Draw a card.'").
        It is CR 117.3c that keeps priority with you - "if a player has
        priority when they cast a spell, activate an ability, or take a
        special action, that player receives priority afterward" - and the
        default is set here because a seat that cycles in its own main phase
        almost always means to keep playing, not to hand the turn over before
        the draw has even resolved. Pass `hold_priority=false` when you really
        do want to release (and note that the cycling ability only resolves
        once someone does) - releasing does not block or wait either, no
        branch of this tool does. See `cast`'s docstring for the answer's
        shape."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        state = seat.fetch_state()
        body: dict[str, Any] = {
            "player": seat.player,
            "ability_index": ability_index,
            "dry_run": dry_run,
        }
        body.update(card_reference(card))
        sources = seat.sources_or_all(mana_sources, state)
        if sources:
            body["mana_sources"] = sources
        if reasoning:
            body["reasoning"] = reasoning
        if dry_run:
            return seat.call("POST", f"/api/games/{seat.game_id()}/actions/cycle", body)

        def perform() -> Any:
            return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/cycle", body)

        return act(
            seat,
            perform,
            hold_priority,
            reasoning=reasoning,
            protocol_version=ctx.protocol_version if ctx is not None else None,
        )

    @server.tool()
    @_guarded(seats)
    def play_land(
        player_uid: str,
        card: str,
        hold_priority: bool = True,
        reasoning: str = "",
        ctx: Context | None = None,
    ) -> str | InputRequiredResult:
        """Play a land from your hand (your one-per-turn allowance, unless an
        effect says otherwise). `card` is the card's name or id.

        HOLDS PRIORITY BY DEFAULT, unlike `cast`. Playing a land is the special
        action of CR 116.2a, and CR 116.3 says "if a player takes a special
        action, that player receives priority afterward" - so releasing here
        throws away the rest of your own main phase. That is not theoretical:
        a seat played Flagstones of Trokair in precombat main, released, and
        concluded it would have to wait until postcombat to cast the one-drop
        the land had just paid for. Pass `hold_priority=false` only when you
        really do mean to hand priority over - which still does not block or
        wait, no branch of this tool does. See `cast`'s docstring for the
        answer's shape."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body: dict[str, Any] = {"player": seat.player}
        body.update(card_reference(card))
        if reasoning:
            body["reasoning"] = reasoning

        def perform() -> Any:
            return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/play-land", body)

        return act(
            seat,
            perform,
            hold_priority,
            reasoning=reasoning,
            protocol_version=ctx.protocol_version if ctx is not None else None,
        )

    @server.tool()
    @_guarded(seats)
    def activate(
        player_uid: str,
        card_id: str,
        ability_index: int,
        targets: str = "",
        mana_sources: str = "",
        sacrificing: str = "",
        discarding: str = "",
        exiling: str = "",
        color: str = "",
        x_value: int | None = None,
        dry_run: bool = False,
        hold_priority: bool = False,
        reasoning: str = "",
        ctx: Context | None = None,
    ) -> str | InputRequiredResult:
        """Activate one ability of a card you control - a permanent's ability
        on the battlefield, OR an ability that functions somewhere else
        entirely because its own cost or effect says so (CR 113.6j, CR
        113.6m): Waker of Waves' `{1}{U}, Discard this card: ...` or Boseiju,
        Who Endures' Channel, both of which only ever work from a hand, not
        the battlefield - the same shape as cycling (CR 702.29a), just
        printed as ordinary card text instead of the rule's own fixed
        wording. This is the one tool for both: do not reach for `cycle`
        unless the state view actually names a cycling ability, and do not
        assume an ability with no `{T}` in its cost must be on a permanent.

        Find the ability's number in your state view's `abilities` list for
        that card (indexing starts at 0). For a permanent on the
        battlefield, intrinsic mana abilities come after printed ones. For a
        card anywhere else - hand, graveyard, exile - the list holds only
        the abilities that actually function from there, renumbered from
        zero; a card can print an ordinary battlefield ability first and an
        off-zone one second (Boseiju's own `{T}: Add {G}` before its
        Channel), and the number that ability carries in a hand view is not
        the number it carries once the card is on the battlefield. Always
        read the number from the same state view you are acting on, never
        carry one over from a different zone.

        `targets` and `mana_sources` are comma-separated ids as with casting.
        `color` is a single letter (W, U, B, R, G or C) and is required when
        the ability offers a choice of color to add - an "any color" source
        such as Birds of Paradise or Mox Diamond, or a dual/shockland; it is
        ignored by abilities that do not need one.

        `sacrificing`, `discarding` and `exiling` are comma-separated card ids
        for an activation cost's own object-naming piece (CR 118.3) - the
        engine decides what a given ability's cost actually requires, this is
        only how you answer it. Goblin Bombardment's "Sacrifice a creature:
        This enchantment deals 1 damage to any target" is the plain case:
        `activate(..., sacrificing="<creature id>", targets="<any target>")`.
        Usually one id each; a cost that names more than one object
        ("Sacrifice two creatures") takes as many as it asks for. Leaving one
        out when the cost needs it is refused with the engine's own count -
        "sacrifice needs N card(s) chosen, 0 given" - never a crash and never
        a silent no-op. `exiling` only works for an ability on the
        battlefield: an ability that functions off the battlefield (Waker of
        Waves, Boseiju's Channel) has no exile-as-cost path yet and refuses it
        by name.

        `x_value` is CR 107.3a's X, for an activation cost that carries one -
        Fiend Artisan's own "{X}{B/G}, {T}, Sacrifice another creature: Search
        your library for a creature card with mana value X or less, put it
        onto the battlefield, then shuffle." Leaving it out when the ability's
        own cost needs it is refused with the engine's own reason - it is
        never silently activated for X=0.

        Mana abilities resolve immediately and never touch the stack
        (CR 605.3b); every other ability goes on the stack and needs priority
        to be released before it resolves. This tool follows that split rather
        than a fixed default, because only the answer says which kind the
        ability was:

        - a MANA ability ALWAYS HOLDS PRIORITY, even with
          `hold_priority=false`. Releasing is half of "all players pass in
          succession while the stack is empty", which ends the step or phase
          (CR 500.2, CR 505.2), and "any unspent mana left in a player's mana
          pool empties" as it does (CR 500.5) - so an auto-pass here could
          bin the mana this very call just made. Spend it, or `pass`
          deliberately to throw it away.
        - any other ability releases priority and then checks once for your
          next moment to act, unless `hold_priority` is true.

        Never blocks or waits, whichever branch it takes - see `cast`'s
        docstring for the full shape of the answer."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body: dict[str, Any] = {
            "player": seat.player,
            "card_id": card_id,
            "ability_index": ability_index,
            "dry_run": dry_run,
        }
        if targets:
            body["targets"] = _parse_ids(targets)
        sources = _parse_ids(mana_sources)
        if sources:
            body["mana_sources"] = sources
        if sacrificing:
            body["sacrificing"] = _parse_ids(sacrificing)
        if discarding:
            body["discarding"] = _parse_ids(discarding)
        if exiling:
            body["exiling"] = _parse_ids(exiling)
        if x_value is not None:
            body["x_value"] = x_value
        if color:
            body["color"] = color
        if reasoning:
            body["reasoning"] = reasoning
        if dry_run:
            return seat.call("POST", f"/api/games/{seat.game_id()}/actions/activate", body)

        def perform() -> Any:
            return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/activate", body)

        return act(
            seat,
            perform,
            hold_priority,
            reasoning=reasoning,
            protocol_version=ctx.protocol_version if ctx is not None else None,
        )

    @server.tool()
    @_guarded(seats)
    def equip(
        player_uid: str,
        card_id: str,
        target: str,
        mana_sources: str = "",
        dry_run: bool = False,
        hold_priority: bool = False,
        reasoning: str = "",
        ctx: Context | None = None,
    ) -> str | InputRequiredResult:
        """Equip: attach an Equipment you control to a creature you control,
        paying its equip cost. CR 702.6a: "Equip [cost]" means "[Cost]: Attach
        this permanent to target creature you control. Activate only as a
        sorcery." - your main phase, with the stack empty (CR 307.5).

        `card_id` is the Equipment; `target` is the creature's id. A legal
        equip is listed in your legal actions as a row of kind `equip`, with
        its cost and its target candidates. `mana_sources` is comma-separated
        source ids; leave it empty to offer all your untapped mana sources.

        This is the tool for equip, not `activate`: equip is a keyword ability,
        so the Equipment's `abilities` list has no index for it.

        The ability uses the stack, so this releases priority and then checks
        once for your next moment to act, unless `hold_priority` is true - the
        Equipment attaches when the ability resolves. Never blocks or waits;
        see `cast`'s docstring for the shape of the answer."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        state = seat.fetch_state()
        body: dict[str, Any] = {
            "player": seat.player,
            "card_id": card_id,
            "target": target,
            "dry_run": dry_run,
        }
        sources = seat.sources_or_all(mana_sources, state)
        if sources:
            body["mana_sources"] = sources
        if reasoning:
            body["reasoning"] = reasoning
        if dry_run:
            return seat.call("POST", f"/api/games/{seat.game_id()}/actions/equip", body)

        def perform() -> Any:
            return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/equip", body)

        return act(
            seat,
            perform,
            hold_priority,
            reasoning=reasoning,
            protocol_version=ctx.protocol_version if ctx is not None else None,
        )

    @server.tool()
    @_guarded(seats)
    def declare_attackers(
        player_uid: str,
        attackers: str,
        hold_priority: bool = False,
        reasoning: str = "",
        ctx: Context | None = None,
    ) -> str | InputRequiredResult:
        """Declare attackers during your declare attackers step. `attackers`
        is a JSON list: [{"card": "<attacker id>", "defender": "<player
        name or planeswalker id>"}]. Declare all attackers at once. Releases
        priority and then checks once for your next moment to act, unless
        `hold_priority` is true - it never blocks or waits; see `cast`'s
        docstring for the shape."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        # No `player`: `AttackRequest` carries no such field, on purpose -
        # `app.action_attack` resolves the declaring seat from the uid and
        # refuses to trust a name off the body (CR 608.2d's "the player
        # announces"). Pydantic used to drop this key without a word; it is
        # a 422 now, so it goes.
        body: dict[str, Any] = {
            "attackers": [
                {"card_id": pair["card"], "defender": pair["defender"]}
                for pair in _parse_pairs(attackers)
            ],
        }
        if reasoning:
            body["reasoning"] = reasoning

        def perform() -> Any:
            return seat.call_json("POST", f"/api/games/{seat.game_id()}/actions/attack", body)

        return act(
            seat,
            perform,
            hold_priority,
            reasoning=reasoning,
            protocol_version=ctx.protocol_version if ctx is not None else None,
        )

    @server.tool()
    @_guarded(seats)
    def declare_blockers(
        player_uid: str,
        blocks: str,
        hold_priority: bool = False,
        reasoning: str = "",
        ctx: Context | None = None,
    ) -> str | InputRequiredResult:
        """Declare blockers during the declare blockers step. `blocks` is a
        JSON list: [{"blocker": "<your creature id>", "blocked": "<attacker
        id>"}]. Declare all blocks at once. Releases priority and then checks
        once for your next moment to act, unless `hold_priority` is true - it
        never blocks or waits; see `cast`'s docstring for the shape."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body: dict[str, Any] = {
            "player": seat.player,
            "blocks": [
                {"blocker": pair["blocker"], "blocked": pair["blocked"]}
                for pair in _parse_pairs(blocks)
            ],
        }
        if reasoning:
            body["reasoning"] = reasoning

        def perform() -> Any:
            return seat.call_json(
                "POST", f"/api/games/{seat.game_id()}/actions/declare-blockers", body
            )

        return act(
            seat,
            perform,
            hold_priority,
            reasoning=reasoning,
            protocol_version=ctx.protocol_version if ctx is not None else None,
        )

    @server.tool()
    @_guarded(seats)
    def decide(
        player_uid: str,
        card_ids: str = "",
        targets: str = "",
        accept: bool | None = None,
        mana_sources: str = "",
        moved: str = "",
        top_order: str = "",
        energy_amount: int | None = None,
        life_amount: int | None = None,
        choice: str = "",
        top: bool | None = None,
        pile_one: bool | None = None,
        usernames: str = "",
        order: str = "",
        damage: str = "",
        generation: int | None = None,
        hold_priority: bool = False,
        reasoning: str = "",
        ctx: Context | None = None,
    ) -> str | InputRequiredResult:
        """Answer your pending decision (CR 608.2d) - `battlefield_state`'s
        `pending_decision` field says what is being asked, by `kind`.

        **Read `pending_decision.answer_with` and paste it.** Every pending
        decision carries the call that answers it, already filled in with
        this question's own `generation` and naming the one parameter that
        carries the answer (`pending_decision.answer_field`) - so the mapping
        below is a reference for *what to choose*, never a lookup you have to
        run before you can answer at all. A `may` arrives with
        `answer_with: "decide(accept=true, generation=7)"`; a `choose_cards`
        with `answer_with: "decide(card_ids=\"<one of pending_decision.cards>\",
        generation=12)"`. Fill in the angle-bracket placeholder, keep the
        parameter name and the generation, send it.

        - `choose_cards` (the cleanup discard, a "discard N" effect, a
          "reveal the top N, choose among them" effect - and **every
          library search**, a fetchland or a tutor): `card_ids` is a
          comma-separated list of ids chosen from `pending_decision.cards`.

          **How many you must name is `pending_decision.exact`, never the
          kind.** `True` means exactly `pending_decision.amount` ids - the
          cleanup discard, a bottoming. `False` means any number from zero
          up to `amount` (CR 107.1c: "if a rule or ability instructs a
          player to choose 'any number,' that player may choose any positive
          number or zero"). When `exact` is `False`, **the empty answer is a
          real answer, not a missing one**: leaving `card_ids` empty finds
          nothing, and that is the legal decline. When `exact` is `True`, an
          empty answer is refused, and so is any count other than `amount`.

          **A search is one choice over the whole zone, not a walk.**
          CR 701.23a: "to search for a card in a zone, look at all cards in
          that zone (even if it's a hidden zone) and find a card that matches
          the given description." So a search arrives as a *single*
          `choose_cards` whose `cards` holds **every legal candidate at
          once**, and your answer names the ones you actually want. You are
          never shown one card at a time and asked to accept or decline it,
          so never treat a candidate you do not want as the whole question:
          name the card you do want.

          Which branch you are in is `exact` again. CR 701.23b - "if a player
          is searching a hidden zone for cards with a stated quality, such as
          a card with a certain card type or color, that player isn't
          required to find some or all of those cards even if they're present
          in that zone" - is `exact` `False`: any 0..`amount` is legal, and
          finding nothing is a choice you may make. CR 701.23d - "if a player
          is searching a hidden zone simply for a quantity of cards, such as
          'a card' or 'three cards,' that player must find that many cards
          (or as many as possible, if the zone doesn't contain that many)" -
          is `exact` `True`: there you choose *which*, never *whether*.

          `pending_decision.source` names the card asking, and it is what
          tells you which find you are entitled to. A Wooded Foothills
          ("{T}, Pay 1 life, Sacrifice this land: Search your library for a
          Mountain or Forest card, put it onto the battlefield, then
          shuffle.") offers every card that matches, and a Breeding Pool is
          one of them - its type line is "Land - Forest Island", so it is a
          Forest card as surely as a basic Forest is. Read `source`, then
          pick the best candidate, not the first.

          **Read `pending_decision.card_labels` to know what the ids are.**
          It is one name per `cards` entry, positionally aligned with it -
          entry N of `card_labels` names entry N of `cards`. For a candidate
          in a hidden zone (CR 400.2: "public zones are zones in which all
          players can see the cards' faces ... graveyard, battlefield, stack,
          exile, ante, and command are public zones" - the library and the
          hand are not among them) your own state carries no detail to look
          the id up against: `battlefield_state` strips every library, including your
          own. `pending_decision.card_details` carries the fuller view of the
          same ids where the server can resolve them, and is worth reading
          for oracle text; `card_labels` is the naming that is always there
          and always aligned. Answering off bare ids is playing the search
          blind, which is how the wrong land gets fetched.

          `pending_decision.default_cards`, when it is not null, is what
          your *silence* costs: the ids the engine takes if this decision
          times out unanswered, in place of the kind's generic default. For a
          search it is the first `amount` candidates in zone order - so an
          unanswered fetch still fetches, but it fetches whatever happened to
          sit nearest the top rather than the card you wanted. Check it
          before deciding a decision is not worth answering.
        - `choose_targets` (a triggered ability's targets, CR 603.3d, or a
          targeted ability being activated with no targets supplied,
          CR 602.2b/601.2c): `targets` is a comma-separated list of up to
          `pending_decision.target_maximum` entries, each a card or ability
          id or a player's name, chosen from `pending_decision.targets` -
          leave it empty to give the ability no target at all, which is
          always legal here (this wave does not yet refuse an empty answer to
          a trigger that requires one).
        - `may`, `pay_life`, `pay_energy`, and `pay_mana` (e.g. a shockland's
          "pay 2 life", CR 119.4; a fixed "pay {E}{E}", CR 107.14; or "counter
          target spell unless its controller pays {N}", CR 118.12a): `accept`
          is `True` to say yes, `False` to say no. A `pay_mana` decision is
          asked of the *targeted spell's controller*, not whoever cast the
          counterspell, and CR 608.2g/CR 605.3a let that payment count both
          mana already floating in that player's pool and the untapped mana
          abilities they could activate right now - see `pending_decision
          .player` for who is actually being asked. `pending_decision
          .affirmative_is_legal` is `False` when yes is not a legal answer at
          all (too little floating mana or too few untapped sources to pay) -
          answering `True` there is refused.

          `pay_mana` alone also takes `mana_sources` (engine PR #17, B6-08):
          comma-separated ids, a subset of `pending_decision.mana_sources` -
          the payer's own untapped permanents - naming which to activate
          while paying, instead of leaving the engine to tap them all. Legal
          only alongside `accept=True`; leave it empty (the default) for the
          plain yes/no this kind has always taken.
        - `pay_energy_amount` (e.g. "pay any amount of {E}", CR 107.14,
          CR 118.3): `energy_amount` is an integer from 0 up to
          `pending_decision.amount` (the energy you actually have). Not the
          same question as `pay_energy`'s `accept` - this kind asks how much,
          not whether. 0 always declines and is always legal.
        - `pay_life_amount` (e.g. Necrodominance's "you may pay any amount of
          life", CR 119.4): `life_amount` is an integer from 0 up to
          `pending_decision.amount` (your life total). The variable-amount
          cousin of `pay_life`'s `accept`, the same way `pay_energy_amount`
          is `pay_energy`'s - and a distinct field from `energy_amount`,
          since the two pay from different resources. 0 always declines and
          is always legal (CR 119.4b).
        - `choose_a_quality` (e.g. Utopia Sprawl's "as this enters, choose a
          color", CR 614.12a): `choice` is the chosen color, card name, or
          creature type. `pending_decision.options` lists the legal colors
          when this is a color choice (the five letter codes); empty
          `options` means a card name or creature type, and any non-empty
          string is accepted - `Game.ask` is what actually enforces
          legality, so a bad card name or creature type still reaches the
          engine rather than being second-guessed here.
        - `choose_a_mode` (e.g. Elder Gargaroth's "whenever this creature
          attacks or blocks, choose one -", CR 603.3c, CR 700.2a): `choice`
          is the chosen mode's exact printed text, one of
          `pending_decision.options` (a modal ability's modes are always
          fully enumerated there, unlike a `choose_a_quality` color choice's
          five letters standing in for an open universe). Shares the
          `choice` field with `choose_a_quality` - both are "which of these
          printed strings did you pick".
        - `scry` and `surveil`: a look-then-split. `moved` is a
          comma-separated list of the `pending_decision.cards` entries you
          opt into; every card not named goes to the other half, in
          `top_order` (comma-separated ids, topmost first) if that half is
          ordered on top, or unchanged otherwise. Leaving `moved` empty is a
          real answer: it opts into nothing and is the decline.

          **How many you must name is `pending_decision.exact`, the same
          check a `choose_cards` answer is held to (CR 608.2d).** `True`
          means exactly `pending_decision.amount` ids - Codecracker Hound's
          "look at the top two cards of your library. Put one into your
          hand and the other into your graveyard" offers no third option,
          so `moved` must name exactly one. `False` means any number from
          zero up to `pending_decision.amount` (CR 107.1c): a plain scry or
          surveil's `amount` equals every card in `pending_decision.cards`
          ("any number of them"), while Devourer of Destiny's "you may put
          one of those cards back on top of your library" is `exact=False`
          with `amount=1` - a real ceiling of one, never "any". `moved`'s
          own `answer_with` template already states the count this
          question enforces; `validate_answer` refuses `moved` outside it.

          **Where the two halves go is read off
          `pending_decision.moved_label` and `pending_decision.kept_label`,
          never off the kind.** They are short phrases in the asking card's
          own words - "top of your library", "exiled", "your graveyard",
          "bottom of your library" - `moved_label` for the cards you named,
          `kept_label` for the ones you did not. When they are present they
          are authoritative; read them before answering, and quote them if
          you explain the choice.

          The kind is not the destination, because the engine reuses the
          `scry` kind for any look-then-split. Devourer of Destiny's
          first-upkeep trigger ("look at the top four cards of your library.
          You may put one of those cards back on top of your library. Exile
          the rest.") arrives as `scry`, and its `moved` half is kept *on
          top* while the rest is *exiled* - the reverse of a scry on both
          halves. `moved` is the pile you opt into, not "the cards that leave
          the top".

          Only when both labels are absent or empty may you assume the
          keyword's own pair: to "scry N" is to put any number of the looked
          at cards on the bottom of your library and the rest on top
          (CR 701.22a), and to "surveil N" is to put any number of them into
          your graveyard and the rest on top (CR 701.25a). That fallback is
          right for a decision scry or surveil itself raised and wrong for
          everything else, so prefer the labels.

          `pending_decision.cards` is still bare ids
          (`moved`/`top_order` answer by id), but `pending_decision.card_details`
          - present only for the deciding player - carries the full card view
          (name, oracle text, etc.) for exactly those ids, so a `scry`/`surveil`
          (a *look*, CR 701.20e: shown only to the deciding player) or a
          library-sourced `choose_cards` is never answered blind. Look the id
          up in `card_details` before choosing.
        - `opening_hand_action` (Leyline of the Void's "if this card is in
          your opening hand, you may begin the game with it on the
          battlefield", CR 103.6a; or a card that lets you reveal it from
          your opening hand, CR 103.6b): `accept` is `True` to take the
          action, `False` to decline - "may take any such actions"
          (CR 103.6), so no is always a real answer. This one arrives before
          the first turn has begun, right after the mulligan process, so it
          may well be the very first thing a seat is asked; answering it is
          what lets the game reach turn one. `pending_decision.source` names
          the card offering it.
        - `put_on_top_or_bottom` (Subtlety's "its owner puts it on the top or
          bottom of their library"): `top` is `True` for the top, `False` for
          the bottom. Not `accept` - this is a top/bottom choice, not a
          yes/no, even though both are booleans. `pending_decision.player` is
          the *targeted spell's owner* here, not Subtlety's controller
          (CR 108.3) - check it before answering, since in a two-player game
          it is easy to assume it is always the caster.
        - `choose_pile` (CR 700.3's pile primitive - Liliana of the Veil's
          own `-6`, "Separate all permanents target player controls into two
          piles. That player sacrifices all permanents in the pile of their
          choice."; Do or Die is the same shape): `pile_one` is `True` to
          choose pile one, `False` for the other pile. `pending_decision
          .cards` names pile one's own contents (the divider's earlier
          choice, a separate `choose_cards` question you already answered or
          the engine already asked someone else); the complement of it,
          among whatever the target player controls, is pile two. Not
          `accept` - a two-way pile choice, not a yes/no, even though both
          are booleans.
        - `choose_players` (CR 701.34a's own "and/or players" half of
          Proliferate - "choose any number of permanents and/or players,
          then give each one a counter"): `usernames` is a comma-separated
          list chosen from `pending_decision.players`. Every printed
          proliferate in the pool offers "any number" (CR 107.1c), so this
          is `exact=False` in practice - leaving `usernames` empty declines
          every player, which is a real, legal answer, not a missing one.
          `choose_players`'s own twin question, a `choose_cards` decision
          over the same resolution's eligible permanents, is asked
          alongside it (CR 701.34a proliferates both halves together) - answer
          both, since proliferating nobody at all only skips the effect
          entirely when neither offers a real choice.
        - `order_triggers` (CR 603.3b: "if multiple abilities have triggered
          since the last time a player received priority ... each player, in
          APNAP order, puts each triggered ability they control on the stack
          in any order they choose"): `order` is a comma-separated
          permutation of exactly `pending_decision.cards` - which for this
          kind holds triggered-ability ids, not card ids - **bottom of the
          stack first**, so the *last* id you list is the ability that
          resolves *first*. Every offered id must appear exactly once;
          dropping or repeating one is refused.
          Do not answer this one off the ids alone. Read each id in
          `pending_decision.card_details` first, the same way you would for a
          `scry`: for this kind that block carries the ability's *source*
          name and the ability's own printed text (CR 113.1c makes an ability
          on the stack an object, so it has no card of its own to name it
          by). When an id is missing from `card_details` - CR 603.3b is asked
          one step before CR 603.3's "its controller puts it on the stack",
          so a batch of triggers can be in no zone yet - fall back to
          `pending_decision.prompt`, which numbers each source positionally
          against `cards`: entry N of the prompt is entry N of `cards`.
        - `assign_combat_damage` (CR 703.4k, 510.1c, 702.19b: how a blocked
          attacker's combat damage divides among its blockers, and, with
          trample, past them to the player or planeswalker it is attacking) -
          only asked when there is a real choice: two or more blockers, or
          trample with power beyond what is needed to kill them all. `damage`
          is a JSON list of `{"target": ..., "amount": ...}` objects, one per
          recipient: `target` is a card id from `pending_decision.cards[1:]`
          (the blockers, in declaration order - `pending_decision.cards[0]`
          is the attacker itself) or, with trample, the defending player or
          planeswalker named in `pending_decision.targets`. Every amount must
          be a non-negative integer, and the amounts must sum to *exactly*
          `pending_decision.amount` (the attacker's power, CR 510.1a) -
          neither more nor less.

          `pending_decision.lethal_damage` is aligned with
          `pending_decision.targets`: it is how much damage each recipient
          still needs before it counts as having lethal damage marked
          (already counting damage marked earlier, deathtouch making 1
          always lethal, and damage other attackers assign this same step) -
          0 for the player or planeswalker entry. Damage may reach the player
          or planeswalker (`pending_decision.trample` true) only once *every*
          blocker has been assigned at least its own `lethal_damage` (CR
          702.19b) - trampling past a blocker that is short is refused.

          An unblocked attacker, or one blocked by exactly one creature with
          no trample excess, never raises this decision at all: CR 510.1c
          leaves nothing to divide there, so the combat damage step runs
          straight through without a pause.
        - `mulligan`, `declare_attackers` and `declare_blockers`: see below.

        `generation` is `pending_decision.generation`, echoed back to say
        WHICH question this answer is for - `answer_with` already carries it,
        so pasting the template opts you in. One resolution can ask two
        decisions in a row (a fetchland's search, then the shockland's "pay 2
        life"), and accepting the first replaces the standing question with
        the second immediately. Without the generation, an answer assembled
        from the earlier read is either applied to the wrong question or
        refused with a flat "no pending decision for you" that says nothing
        about why; with it, the refusal names both generations and tells you
        to re-read. Omit it only if you have no read to name.

        `may`, `pay_life`, `scry`, and `surveil` were not answerable through
        this tool before this wave: the request simply parked until its
        timeout and the engine's documented default was taken instead,
        silently discarding the choice (see `decisions.py`'s module docstring
        on the API side for the CR 608.2d fallback rule).

        `mulligan`, `declare_attackers`, and `declare_blockers` are answerable
        through this same tool too, exactly like every other kind - there is
        no longer a decision kind this tool cannot answer. `declare_attackers`/
        `declare_blockers` are also reachable through the dedicated
        `attackers`/`blockers` actions, which build the same pair-shaped
        answer this tool takes for those two kinds and resume the identical
        paused decision - use whichever reads more naturally for the call
        you are making, they are two doors onto the same thing, not two
        different mechanisms. `mulligan` reaches you through
        `pregame/mulligan`'s own declaration for your own seat during a
        normal game boot (CR 103.5) - this tool's `mulligan` kind is what you
        answer only if an admin's `pregame/settle` call is what actually put
        the question to you (and only when that call was made with
        `use_defaults` left `False`, its default - a `use_defaults=True`
        settle never pauses at all, it pre-fills every such question with
        the engine's own default before the call returns), which is not the
        ordinary play path. Answering when nothing is pending, or when the
        pending decision is not yours, is refused rather than accepted.

        Answering a decision does not, itself, involve priority (CR 608.2d is
        the resolving effect asking a question, not a priority action), but
        follows the same shape as every other tool here for one reason:
        answering it is very often what was blocking your own next turn at
        priority, so waking straight back into whatever comes next saves the
        extra round trip. Releases priority and then checks once for your
        next moment to act, unless `hold_priority` is true - it never blocks
        or waits."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body: dict[str, Any] = {
            "player": seat.player,
            "card_ids": _parse_ids(card_ids),
            "targets": _parse_ids(targets),
            "moved": _parse_ids(moved),
        }
        if accept is not None:
            body["accept"] = accept
        sources = _parse_ids(mana_sources)
        if sources:
            body["mana_sources"] = sources
        if top_order:
            body["top_order"] = _parse_ids(top_order)
        if energy_amount is not None:
            body["energy_amount"] = energy_amount
        if life_amount is not None:
            body["life_amount"] = life_amount
        if choice:
            body["choice"] = choice
        if top is not None:
            body["top"] = top
        if pile_one is not None:
            body["pile_one"] = pile_one
        if usernames:
            body["usernames"] = _parse_ids(usernames)
        if order:
            body["order"] = _parse_ids(order)
        if damage:
            body["damage"] = [
                {"target": entry["target"], "amount": entry["amount"]}
                for entry in _parse_pairs(damage)
            ]
        if generation is not None:
            body["generation"] = generation
        if reasoning:
            body["reasoning"] = reasoning

        def perform() -> Any:
            answer = seat.call_json("POST", f"/api/games/{seat.game_id()}/decide", body)
            if isinstance(answer, dict) and answer.get("accepted") is False:
                return {"allowed": False, "reason": answer.get("reason"), **answer}
            return {"allowed": True, **answer} if isinstance(answer, dict) else answer

        return act(
            seat,
            perform,
            hold_priority,
            reasoning=reasoning,
            protocol_version=ctx.protocol_version if ctx is not None else None,
        )

    @server.tool(name="pass")
    @_guarded(seats)
    def pass_once(
        player_uid: str, reasoning: str = "", ctx: Context | None = None
    ) -> str | InputRequiredResult:
        """Pass priority once - "I am done for now". The whole of what a player
        does with priority besides acting.

        **Use `pass_until` instead whenever you have nothing to do until some
        later point in the turn - a step name, and it crosses the whole
        stretch in one call.** Calling this one again and again through every
        window in between is the wrong move: each call is a full round trip
        and a whole agent turn, so walking a turn that way is slow, expensive,
        and exactly the log noise a repeated single pass produces. Reach for
        this tool only when you genuinely hold priority once - in response to
        something, with your next action already decided for the very next
        window - not as a substitute for `pass_until`.

        CR 117.1 lists what the player with priority may do: "cast spells,
        activate abilities, and take special actions". CR 117.3d is the rest of
        it: "If a player has priority and chooses not to take any actions, that
        player passes ... Then the next player in turn order receives priority."
        There is no other priority move, and this tool is it.

        What follows from a pass is not yours to do, and there is no tool for
        it - CR 117.4: "If all players pass in succession ... the spell or
        ability on top of the stack resolves or, if the stack is empty, the
        phase or step ends." So `outcome` tells you which of the two your pass
        caused:

        - `outcome: "resolve_top"` - both seats had passed with something on
          the stack, so its top object resolved and priority goes round again;
        - `outcome: "end_step"` - both seats had passed on an empty stack, so
          the step ended and the next one has already begun. The game rolls
          itself through any step nobody receives priority in (untap, and
          cleanup unless CR 514.3a's exception applies - CR 500.3), and
          `settled_through` lists the steps it crossed;
        - `outcome: "passed"` - it is simply the other seat's turn to decide.

        Checks once for your next moment to act and returns immediately -
        never blocks (poll-and-return, `seatkit.block_until_wake`'s own
        docstring: MCP 2026-07-28 removed resumability). Call again to keep
        checking, the same as any other action tool. There is no
        `hold_priority` here - passing already is giving priority up - and a
        refusal (it was not your turn to decide) returns immediately either
        way. A decision this very pass raises for you comes back shaped
        `input_required` rather than plain text - see `wait_for_priority`'s own
        docstring.

        `reasoning`: why you are done here, in your own words - required for an
        agent seat.

        Use `pass_until` instead for a stretch of windows you cannot act in;
        use this one where the pass itself is part of the game."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body: dict[str, Any] = {"player": seat.player}
        if reasoning:
            body["reasoning"] = reasoning

        def perform() -> Any:
            return seat.call_json(
                "POST", f"/api/games/{seat.game_id()}/actions/pass-priority", body
            )

        return act(
            seat,
            perform,
            hold_priority=False,
            release=False,
            reasoning=reasoning,
            protocol_version=ctx.protocol_version if ctx is not None else None,
        )

    @server.tool()
    @_guarded(seats)
    def pass_until(
        player_uid: str,
        step: str,
        whose_turn: str = "mine",
        resume: str = "",
        stop_on_stack: bool = True,
        stop_when_attacked: bool = True,
        stop_when_you_can_attack: bool = True,
        reasoning: str = "",
        max_seconds: float = 0.0,
        arm: str | list[str] = "",
        arm_reasoning: str = "",
        opposing_archetype: str = "*",
        inputs: dict[str, Any] | None = None,
        ctx: Context | None = None,
    ) -> str | InputRequiredResult:
        """Keep passing priority - one `pass` after another, automatically -
        until something worth looking at happens. The same verb, run to a
        destination instead of one window at a time.

        ## Where it is going: `step` and `whose_turn`

        `step` is the point in the turn to run to, and `whose_turn` says whose
        turn that step has to be in: "mine" (default), "opponent", or "any" for
        whichever comes first. Legal steps, in turn order:

            upkeep, draw, precombat_main, beginning_of_combat,
            declare_attackers, declare_blockers, combat_damage,
            end_of_combat, postcombat_main, end_step

        (`untap` and `cleanup` are not offered: no player receives priority in
        either, CR 502.4 and CR 514.3, so a run could never stop there. The
        game crosses both on its own - CR 500.3.)

        Naming the step you are standing in right now means its NEXT
        occurrence, never this one: `step="upkeep"` asked during your upkeep
        passes a whole turn round to your next one, which is what "until my
        upkeep" means when you say it out loud.

        It passes every priority you currently hold - one after another, with
        no waiting between them, each paid for by a pass that already went
        through - and would otherwise return the moment it has to wait for
        your opponent. **`max_seconds` is where that wait can actually happen
        now, bounded**: left at its default `0.0`, this returns immediately
        the moment it would have to wait, exactly as it always has. Passed a
        positive value (capped at `seatkit.MAX_WAIT_SECONDS`), a hand-back
        that is genuinely just waiting on the opponent is retried on the
        server for up to that long before giving up - so a stretch that used
        to need a `Bash sleep` plus a second call with `resume` can often
        finish, or get further, in one call. It still always returns on its
        own, never past the bound (the same SEP-2577 reasoning
        `wait_for_priority`'s own `max_seconds` explains - MCP 2026-07-28
        removed resumability, so nothing here holds a connection open
        indefinitely).

        `finished` says which kind of answer you got:

        - `finished: true` - a real stop condition fired, and `stopped_because`
          says which: the destination is reached; a decision is addressed to
          you (CR 608.2d), which arrives shaped `input_required`; something new
          landed on the stack; it is your own declare-attackers step; you are
          being attacked and it is your declare-blockers step; a pass was
          refused; the game ended; the game is gone (released or deleted -
          `stopped_because` starts `the game is gone`, and there is nothing
          left to play in it).
        - `finished: false` - the run got as far as it could (within
          `max_seconds`, if you gave one) and handed the board back, because
          your opponent holds priority or the game is frozen on a question
          only they can answer. The answer carries a `resume` token; **pass it
          back verbatim as `resume` on your next call, with `max_seconds`
          again**, and the run continues as one run. Without `resume` the next
          call starts a fresh run stamped where you are standing right now,
          which would make it skip the very combat step it is supposed to stop
          you at (the exemption for "the step the run began in" - see below).

        `passes` counts the priorities it actually gave up, in this call.

        A combat stop never fires on the step the run *started* in, the same
        way the destination never does: asking to auto-pass past combat while
        already standing in that step is you having looked at combat and moved
        on, and stopping there would make the call do nothing at all.

        ## The three stop conditions you can switch off

        These are not destinations and they do not replace `step` - they are
        extra reasons to stop *on the way there*, and all three default to true
        because each defends against something you cannot see going wrong. They
        are the same three ticks the human player has in their own stops panel,
        and they mean the same thing there, so you and your opponent never stop
        in different places.

        - `stop_on_stack=false` runs straight through anything the opponent
          casts or any trigger that goes on the stack. **This is the one that
          gives you your chance to respond**: a spell cast in a window you were
          passing through - your opponent's instant in your upkeep while you
          were running to your own precombat main - is only answerable while
          the stack still holds it, and CR 117.4 resolves it the moment all
          players have passed in succession. Left on (the default), the run
          hands the board back with the spell still on the stack and priority
          still yours. Switch it off only for a stretch where you have decided
          you are responding to nothing - no instant, no activated ability,
          nothing worth holding up.
        - `stop_when_attacked=false` runs straight through your own
          declare-blockers step. **You will block nothing and take the whole
          attack.** Declaring blockers is a turn-based action that "doesn't use
          the stack" (CR 509.1), so nothing lands on the stack when you are
          attacked and `stop_on_stack` cannot catch it for you. Switch this off
          only when you have genuinely decided not to block - an empty board, or
          a life total the attack cannot reach. The run stops whenever you are
          the defending player in a declare-blockers step with at least one
          attacker declared; it does not try to work out whether any block would
          be *legal* (evasion, menace, protection, CR 509.1b) - that is the
          engine's job, and you decide what to do with the stop.
        - `stop_when_you_can_attack=false` runs straight through your own
          declare-attackers step. **You will attack with nothing that turn.**
          Declaring attackers is a turn-based action that "doesn't use the
          stack" too (CR 508.1), so again nothing lands on the stack to catch.
          This costs you an attack rather than the game, and the reason it
          still stops by default is that skipping it is *silent*: the engine
          treats a player who never declares as declaring nobody, which is
          always legal, so the turn simply produces no attack and nothing
          anywhere tells you why. Leaving it on means a run aimed past combat -
          `step="postcombat_main"`, `step="end_step"` - stops in your
          declare-attackers step every one of your turns. That is the point:
          you are being asked before the attack goes by. Switching it off is
          the deliberate statement "I am not attacking this turn", not a way to
          make the call quieter. It does not check whether you control a
          creature that could legally attack (summoning sickness, defender, tap
          state, CR 508.1's restrictions and requirements) - that is the
          engine's job too.

        ## Passing by hand is still a play

        Do not reach for this every time. A pass your opponent watches you make
        is information: holding priority with mana up represents a trick whether
        or not you have one, and auto-passing a window where they expect a flash
        threat throws that away. Auto-passing every time also tells them "I have
        nothing" just as reliably. Use this for the stretches where you
        genuinely cannot act, and pass by hand where the pass itself is part of
        the game.

        `reasoning`: why you are giving up this whole stretch, in your own
        words - said once, for the run, and threaded onto every pass it
        makes.

        ## Arming (S2)

        `arm`: a list of registered script ids (or a comma-separated string of
        them), in priority order, to fire on their own the instant this run finishes here - or
        hits its own `MAX_PASSES_PER_CALL` hand-back - never on a plain
        "waiting for the opponent" hand-back, which is not this seat's moment.
        Judged the same way `run_script`/the hint judge; two armed scripts
        applying at once is a conflict and neither fires (`armed_conflict`
        names them). `arm_reasoning` is required whenever `arm` is non-empty.
        `opposing_archetype` and `inputs` mean what they mean for
        `run_script`. Never mandatory - left empty (the default), this call
        behaves exactly as it always has. Refused outright to an advisor, same
        as this whole tool."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        problem = seatkit.autopass_target_error(step, whose_turn)
        if problem:
            return problem
        try:
            resumed = json.loads(resume) if resume else None
        except ValueError as exc:
            return (
                f"`resume` is not JSON: {exc}. Hand it back exactly as the "
                "previous answer gave it to you, or leave it out to start a new run."
            )
        if resumed is not None and not isinstance(resumed, dict):
            return (
                "`resume` must be the object a previous `pass_until` answer "
                "carried under `resume`, handed back unchanged."
            )
        # Through `_tool_result`, not `json.dumps`: a run stops *on* a decision
        # addressed to this seat (`seatkit.pass_until` returns the moment
        # `autopass_stop_reason` sees one, with the whole state under
        # `"state"`), which is exactly the case `InputRequiredResult` exists
        # for. This was the one action tool returning raw text on that path, so
        # the one tool most likely to be mid-run when a decision arrives was
        # also the only one that could not say so on the wire.
        return _tool_result(
            seat,
            seatkit.pass_until(
                seat,
                until_step=step,
                whose_turn=whose_turn,
                stop_on_stack=stop_on_stack,
                stop_when_attacked=stop_when_attacked,
                stop_when_you_can_attack=stop_when_you_can_attack,
                resume=resumed,
                reasoning=reasoning or None,
                max_seconds=max_seconds,
                should_stop=cancel_probe(ctx),
                corpora=seats.script_corpora,
                arm=_parse_ids(arm),
                arm_reasoning=arm_reasoning,
                opposing_archetype=opposing_archetype,
                inputs=inputs,
            ),
            ctx.protocol_version if ctx is not None else None,
        )

    @server.tool()
    @_guarded(seats)
    def run_script(
        player_uid: str,
        script: str,
        opposing_archetype: str = "*",
        *,
        reasoning: str,
        resume: str = "",
        wait: bool = True,
        max_seconds: float = 0.0,
        inputs: dict[str, Any] | None = None,
        ctx: Context | None = None,
    ) -> str | InputRequiredResult:
        """Run one of your registered play scripts: its moves, in order, and
        the one `next` script that applies after them.

        `script` is the script's `id`. The runner waits for the script's
        `when` (passing priority with every stop on, the way `pass_until`
        does), checks `opposing_archetypes` and its `true`/`false`
        conditions against the board, makes each move through the same
        routes a hand-played move uses - so every refusal is the engine's own
        - answers each decision a move raises from the move's `answers`, and
        passes priority only over its own objects until they resolve. It
        never passes beyond the resolution of its last move.

        `reasoning`: why you are running this script now, in your own words.
        Every move it makes is sent with it; without it the run is refused.

        `opposing_archetype`: the opponent's archetype as you read it, `*`
        (the default) when you do not know. A script listing only other
        archetypes does not apply. `inputs`: the facts your deck's scripts
        read that the board cannot show, by name - `{"prefer_tempo": true}`.
        `opposing_archetype` is one of them and may be given here instead;
        naming two different archetypes is refused.

        `wait=false` does not wait for the script's `when`: a script off its
        step is simply not applicable. `max_seconds` caps this call
        (at most `seatkit.MAX_WAIT_SECONDS`, which a 0.0 leaves it at).

        The answer is `script_exit` beside the narrowed `board`:
        `script_exit.exit` is one of `chain_complete` (the moves are done,
        and no next script applies), `not_applicable` (with
        `failing_condition`: the `list` and `index` that stopped it and what
        it `seen`), `needs_input` (a required input is missing, named in
        `needs_input`), `invalid_input` (a supplied input is not a value the
        deck declares, with `invalid_input`: its `name`, `value` and
        `reason`), `unforeseen` (with `stop_reason`), `interrupted`
        (someone else acted), `ambiguous` (two cards or two next scripts
        tie), `refused` (with `refusal`, the server's or the runner's words),
        `stale`, `unknown_outcome`, `needs_plan` (call `declare_plan`
        first), `already_ran` (this script already ran this turn and step)
        or `advised` (an advisor's run: move 1 is proposed as advice and the
        rest are named in `unvalidated`). `moves_done`/`moves_total` count
        its moves. `exit` is null when the time cap came first: pass
        `script_exit.resume` back, verbatim, as `resume` to carry on with the
        same run. A decision left pending for you arrives shaped
        `input_required`, like every action tool's."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        answer = script_tools.run(
            seat,
            seats.script_corpora,
            script=script,
            reasoning=reasoning,
            opposing_archetype=opposing_archetype,
            resume=resume,
            wait=wait,
            max_seconds=max_seconds,
            inputs=inputs,
            should_stop=cancel_probe(ctx),
            invoked_via="mcp",
        )
        if isinstance(answer, str):
            return answer
        return _tool_result(seat, answer, ctx.protocol_version if ctx is not None else None)

    @server.tool()
    @_guarded(seats)
    def list_scripts(
        player_uid: str, opposing_archetype: str = "*", inputs: dict[str, Any] | None = None
    ) -> str:
        """Your registered play scripts, highest priority first, each judged
        against the board right now - nothing is run and nothing recorded.

        Each script says whether it `applies`, and when it does not, its
        `failing_condition`: the `list` that stopped it (`priority` for a
        disabled script, `opposing_archetypes`, `when`, `true`, `false`, or
        `hypothetical` for a script whose selectors need engine-computed
        facts this listing could not fetch - `seen` then says why), the
        `index` within it, and what it `seen`. `needs_input` names a
        required input you did not supply. `conflicts` are the
        `play-conflict`/`next-conflict` warnings `plays check` raises that
        involve it - scripts that could both apply at one decision point.
        `runtime_conflict` is true when more than one listed script applies
        right now; `applicable` names them.

        `opposing_archetype` and `inputs` mean what they mean for
        `run_script`, so a listing shows what a run with the same ones would
        find."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        answer = script_tools.listing(
            seat, seats.script_corpora, opposing_archetype=opposing_archetype, inputs=inputs
        )
        return answer if isinstance(answer, str) else json.dumps(answer, indent=1)

    @server.tool()
    @_guarded(seats)
    def mulligan(player_uid: str, take: bool, bottom: str = "", reasoning: str = "") -> str:
        """Declare whether you take a mulligan (CR 103.5), before the game starts.

        A game dealt from decklists rests in the mulligan step until every seat
        has declared - `battlefield_state`'s `pregame` block says who is still owed.
        `take=true` shuffles your hand back and draws a fresh seven;
        `take=false` keeps what you have.

        Keeping costs one card on the bottom of your library per mulligan you
        took (`pregame.cards_to_bottom`). `bottom` is a comma-separated list of
        that many card ids from your hand; leave it empty to let the back of
        your hand go. Does not block.

        An `HTTP 409` with `overruled` means an automatic backstop had already
        declared for this seat because it had gone quiet, and this declaration
        was NOT applied. Retrying cannot help and nothing you can send will
        change it - if you meant to mulligan, that decision is gone and you are
        holding the dealt hand; say so with `report_problem` and play on."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        ids = _parse_ids(bottom)
        body: dict[str, Any] = {"player": seat.player, "take": take}
        if ids:
            body["bottom"] = ids
        if reasoning:
            body["reasoning"] = reasoning
        return seat.call("POST", f"/api/games/{seat.game_id()}/pregame/mulligan", body)

    @server.tool()
    @_guarded(seats)
    def opening_hand_action(player_uid: str, card: str = "", reasoning: str = "") -> str:
        """Take a card's opening-hand action, or finish (CR 103.6).

        Some cards let you act with them from your opening hand - Leyline of
        the Void's "you may begin the game with this card on the battlefield",
        Gemstone Caverns' reveal. `battlefield_state`'s `pregame.opening_hand_options`
        lists what yours offers, and `pregame.opening_hand_actor` says whose
        turn it is.

        `card` is one card id from that list. Call with no `card` when you are
        done - that passes to the next seat, and the last seat finishing starts
        turn 1. Does not block.

        An `HTTP 409` with `overruled` means an automatic backstop had already
        declined this window for the seat because it had gone quiet, and your
        action was NOT taken - the card is still in your hand and will stay
        there. Retrying cannot reopen the window; report it and play on."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        base = f"/api/games/{seat.game_id()}/pregame/opening-hand"
        if not card.strip():
            body: dict[str, Any] = {"player": seat.player}
            if reasoning:
                body["reasoning"] = reasoning
            return seat.call("POST", f"{base}/finish", body)
        body = {"player": seat.player, "card_id": card.strip()}
        if reasoning:
            body["reasoning"] = reasoning
        return seat.call("POST", f"{base}/action", body)

    @server.tool()
    @_guarded(seats)
    def declare_plan(
        player_uid: str,
        board_read: str,
        my_role: str,
        plan: str,
        expecting: str,
        because: str,
        confidence: str = "medium",
        for_turn: str = "current",
    ) -> str:
        """Declare, before you act, how you read the turn you are about to
        take (`POST .../plan`). Call this once at the top of every one of
        your own turns, before your first real action of it: an agent seat's
        first mutating action of its own turn is refused - 422, naming
        `plan` - until this has been called for that turn.

        `board_read`: what you actually see on the board right now, in your
        own words. `my_role`: your role in this matchup - beatdown or
        control, what your deck is trying to do, what losing looks like.
        `plan`: what you intend to do this turn, and why that follows from
        the two above. `expecting`/`because`/`confidence`
        (`"low"`/`"medium"`/`"high"`, default `"medium"`): what you expect
        your opponent to do in response, and the read that supports it - the
        part that makes this evidence rather than commentary.

        `for_turn` (`"current"`, the default, or `"next"`): plan ahead.
        Call this with `for_turn="next"` during your OPPONENT's turn to
        declare for your own next turn, rather than waiting until your
        upkeep and holding priority there to write one - your opponent is
        not waiting on you while you do this, and should not have to. Called
        with `for_turn="next"` while your own turn is already under way,
        this is refused (409, `own_turn_in_progress`): declare once per
        turn, and use the default `"current"` for the turn already in front
        of you.

        This is written to an append-only ledger before your opponent's next
        move, and cannot be edited afterward to fit whatever they actually
        did. After the game, a human reads it next to what your opponent
        actually did - you are not scored, there is no grade, and nothing
        here is graded against you. The only thing that makes it worth
        reading is that it is honest: state the concrete call you actually
        expect, not one vague enough to never be wrong. A prediction that
        cannot be wrong proves nothing either way; an honest wrong read is
        worth far more than a hedge.

        Mutates no `Game` and never blocks. Every field but `for_turn` is
        required and stripped of surrounding whitespace - blank is refused
        the same as missing."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        result = seat.declare_plan(
            board_read=board_read,
            my_role=my_role,
            plan=plan,
            expecting=expecting,
            because=because,
            confidence=confidence,
            for_turn=for_turn,
        )
        return json.dumps(result, indent=1, sort_keys=True)

    @server.tool()
    @_guarded(seats)
    def stage_advice(player_uid: str, step: str, whose_turn: str = "mine") -> str:
        """S2 A6: arm your *next* committing action call to be captured as a
        draft for the window `(step, whose_turn)`, instead of posted as a
        proposal right away - so you can think and act now, while your human
        does not yet hold priority, and have it posted for them the instant
        they do, rather than starting from scratch only once they hold it.

        `step`/`whose_turn` mean exactly what they mean for `pass_until`: one
        of `upkeep, draw, precombat_main, beginning_of_combat,
        declare_attackers, declare_blockers, combat_damage, end_of_combat,
        postcombat_main, end_step`, and `"mine"` (default), `"opponent"`, or
        `"any"`. Naming the step your human is standing in right now means its
        NEXT occurrence, never this one - the same next-occurrence rule
        `pass_until` follows.

        The very next committing call you make (`cast`, `activate`, `pass`,
        ...) after this is what gets captured - probed exactly as an ordinary
        proposal is, just held back rather than posted. Every one of your
        calls after that (this one included, and `wait_for_priority`'s own
        wait) carries `draft: {"status": ...}` while one is staged, and it
        posts on its own the moment your human's priority reaches that window
        - see `wait_for_priority`'s own docstring for the three ways a stale
        draft is dropped instead.

        Staging again before the armed slot is filled in replaces it outright;
        there is only ever one draft in flight. Refused outright for a seat
        (there is no proposal for it to hold back) and for a step/whose_turn
        `pass_until` would also refuse. Mutates no `Game` and never blocks."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        result = seat.stage_advice(step, whose_turn)
        return json.dumps(result, indent=1, sort_keys=True)

    @server.tool()
    @_guarded(seats)
    def report_problem(player_uid: str, text: str) -> str:
        """Report a problem with the engine straight into the game log: write
        what you did, what the rules say should have happened, and what
        happened instead. Use it the moment something looks wrong, then carry
        on - also write the same problem to your problems.md."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        body = {"player": seat.player, "text": text}
        return seat.call("POST", f"/api/games/{seat.game_id()}/report", body)

    @server.tool()
    @_guarded(seats)
    def game_log(player_uid: str, limit: int = 40, since: int = 0) -> str:
        """The game's action log **in full**: everything both players did or
        tried, allowed and refused, newest last, each entry carrying the whole
        board snapshot of its moment - your own view, though: entries touching
        the opponent's hidden zones have their card names blanked the same way
        battlefield_state hides them.

        **You usually do not need this.** Every answer that carries the board
        - `battlefield_state`, `wait_for_priority`, `pass_until`, and every action tool
        - already carries a `recent_log` tail of the same history alongside
        it, so asking "what happened" separately from "what can I do" spends a
        whole extra call and a whole extra turn on something you were handed
        for free. Reach for this one when that tail says `omitted` above zero,
        when you want further back than it goes, or when you need an entry's
        full snapshot.

        `limit` keeps only the last N entries. `since` is a `seq` you already
        have - typically the `last_seq` from a `recent_log` - and asks for
        only what is newer than it; `last_seq` in the answer is where to
        resume from next time."""
        seat = resolve(player_uid)
        if seat is None:
            return NO_SUCH_SEAT
        path = f"/api/games/{seat.game_id()}/log"
        answer = seat.http.get(path, params={"uid": seat.uid, "since": since})
        try:
            payload = answer.json()
            entries = payload.get("log", [])[-limit:]
            return json.dumps(
                {"entries": entries, "last_seq": payload.get("last_seq", since)},
                indent=1,
                sort_keys=True,
            )
        except ValueError:
            return answer.text

    return server


#: 2b T11: the default. A CLI-spawned per-game subprocess (`deploy/mcp.base.json`,
#: today's docker-compose flow) needs `--transport stdio` explicitly; a
#: caller that says nothing gets the network-facing shape this project is
#: moving toward - one long-running server any game's agent can reach,
#: rather than one process per game.
DEFAULT_PORT = 8100


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--game",
        default=None,
        help=(
            "game id; default in stdio mode: the one live game. In http mode, "
            "leaving this unset resolves each call's game from its own seat "
            "uid instead (GET /api/seats/{uid}) - the server may be serving "
            "several live games at once."
        ),
    )
    parser.add_argument("--api", default=DEFAULT_API, help=f"API base url; default {DEFAULT_API}")
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="http",
        help=(
            "stdio: a per-game subprocess, spoken over this process's own "
            "stdin/stdout (the historical shape). http (default): serve the "
            "MCP SDK's stateless streamable-HTTP ASGI app on --host/--port."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="http transport bind host")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"http transport bind port; default {DEFAULT_PORT}",
    )
    return parser


def _serve_http(server: MCPServer, host: str, port: int) -> None:
    """The SDK's stateless streamable-HTTP ASGI app, served over uvicorn.
    `stateless_http=True`: no MCP session is pinned to one HTTP connection,
    which is what lets this one process answer calls for any seat of any
    live game rather than a single negotiated client. A thin, separately
    testable seam - `main()` monkeypatches this in `tests/
    test_mcp_transport_flag.py` rather than actually binding a socket in a
    fast unit test; `tests/test_mcp_http_transport.py` proves the real thing
    over a real socket."""
    import uvicorn

    uvicorn.run(server.streamable_http_app(stateless_http=True), host=host, port=port)


def main() -> None:
    args = _build_parser().parse_args()
    # http mode may be fronting several live games at once, so "the one live
    # game" (stdio's auto-discovery) is only correct when `--game` pins one
    # explicitly - see `Seats.seat_for`/`_seat_for_any_game`.
    resolve_by_uid = args.transport == "http" and args.game is None
    seats = Seats(args.api, args.game, resolve_by_uid=resolve_by_uid)
    # Best-effort heads-up only: NOT fatal. The real, authoritative check is
    # `_guarded` on every tool call below - this process is spawned once for
    # a whole session and must survive the API not being up yet (or not up
    # again yet, mid-restart) at the exact moment it boots.
    trouble = seatkit.api_trouble(seats.http, seats.api)
    if trouble:
        print(f"mcp: starting anyway - {trouble}", file=sys.stderr)
    server = build_server(seats)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        _serve_http(server, args.host, args.port)


if __name__ == "__main__":
    main()
