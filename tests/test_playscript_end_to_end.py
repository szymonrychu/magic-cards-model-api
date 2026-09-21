"""Play scripts end to end: the vendored mtg-decks corpus, registered through
the API and run through the in-process MCP server against the real engine.

Every script here is read from `tests/fixtures/guides/decks/<slug>/plays/`,
the verbatim slice pinned by `MTG_DECKS_SHA` (see that directory's README),
and registered as its deck's whole corpus with the real
`PUT .../seats/{uid}/scripts` route. Boards are staged from real printings
through `POST .../cards`; every move, pass and decision goes through the real
routes into the real engine. Nothing in front of the engine is faked: the
opponent's passes and its response are real HTTP calls from a second client.

The seed sweep at the bottom stages a board for every play file in the slice
out of that deck's own decklist, reading only what the play itself says it
needs - its `when`, its conditions and its moves - so a play a later refresh
adds is picked up without an edit here.
"""

from __future__ import annotations

import functools
import json
import re
import threading
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from conftest import add_card, force_turn_position, life_of, state
from fastapi.testclient import TestClient
from magic_cards_model.machines import Step
from playscript_testkit import FLASH_PERMANENT, advice_play, flash_permanent_play
from test_cast_cost_choices import answer
from test_mcp_input_required import call_tool, wired
from test_playscript_runtime import finished, opponent_passes, seat_uid

from magic_cards_model_api import audit, scryfall, seatkit
from magic_cards_model_api.main import app
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.playscripts import report, runtime
from magic_cards_model_api.playscripts.selectors import compare, matches, parse_type_line
from magic_cards_model_api.repository import Advisor
from magic_cards_model_api.store import store
from mtg_agent.driver.guides import parse_decklist

TESTS = Path(__file__).resolve().parent
GUIDES = TESTS / "fixtures" / "guides"
#: Every deck's `decklist.txt` (the guides slice carries only six of them).
DECKLISTS = TESTS / "driver" / "fixtures" / "mtg-decks"
#: The pin both slices share, read from where it is written down.
GUIDES_REVISION = re.search(
    r'^MTG_DECKS_SHA = "([0-9a-f]{40})"$',
    (TESTS / "driver" / "test_guides.py").read_text(),
    re.MULTILINE,
).group(1)
#: `boros-aggro`'s `open-fetch-unlocks`, the M2 regression play ("a fetch's
#: own choose_cards pick needs a library land's unlocks"). Used to be vendored
#: separately under `ahead-of-pin/` because the pin at the time did not carry
#: it yet; the pin refreshed to d681d42 does, so it is read straight off the
#: pinned slice like every other play now - see `tests/fixtures/guides/README.md`
#: ("Plays ahead of the pin" is empty as of this refresh).

ME, OPPONENT = "alice", "bob"
REASONING = "thin the deck and fix my colours"
PLAN = {
    "board_read": "a fetchland up, no red source untapped, opponent empty-handed on board",
    "my_role": "beatdown - I need both colours online to curve out",
    "plan": "crack the fetch for the shock I am missing, then deploy threats",
    "prediction": {
        "expecting": "bob passes with nothing to do on my main phase",
        "because": "bob has no untapped mana and nothing on the stack",
        "confidence": "medium",
    },
}


@pytest.fixture(autouse=True)
def _never_available_actions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner builds every body from the state view and never pays for
    `available_actions`, end to end as much as in its own tests."""

    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("a script tool called available_actions")

    monkeypatch.setattr(seatkit, "available_actions", refuse)


# -- the corpus -------------------------------------------------------------------


def plays_of(slug: str) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text())
        for path in sorted((GUIDES / "decks" / slug / "plays").glob("*.json"))
        if path.name != "inputs.json"
    ]


def declared_inputs(slug: str) -> list[dict[str, Any]]:
    path = GUIDES / "decks" / slug / "plays" / "inputs.json"
    return list(json.loads(path.read_text())["inputs"]) if path.is_file() else []


def play_of(slug: str, script_id: str) -> dict[str, Any]:
    return next(doc for doc in plays_of(slug) if doc["id"] == script_id)


def register_corpus(
    client: TestClient,
    game_id: str,
    uid: str,
    slug: str,
    *extra: dict[str, Any],
    inputs: list[dict[str, Any]] | None = None,
) -> None:
    """`slug`'s whole vendored corpus (plus `extra`), through the PUT route."""
    body = {
        "deck_slug": slug,
        "guides_revision": GUIDES_REVISION,
        "scripts": [*plays_of(slug), *extra],
        "inputs": declared_inputs(slug) if inputs is None else inputs,
    }
    response = client.put(
        f"/api/games/{game_id}/seats/{uid}/scripts", params={"uid": uid}, json=body
    )
    assert response.status_code == 200, response.text


def seed_cases() -> list[Any]:
    return [
        pytest.param(
            path.parent.parent.name, path.stem, id=f"{path.parent.parent.name}/{path.stem}"
        )
        for path in sorted(GUIDES.glob("decks/*/plays/*.json"))
        if path.name != "inputs.json"
    ]


#: Every vendored location a play script can live in: the pinned slice, its
#: driver-fixtures mirror, and the ahead-of-pin overflow (see
#: `tests/fixtures/guides/README.md`'s "Plays ahead of the pin" - empty as of
#: this refresh, but a future one can add to it again before the next pin
#: catches up).
VENDORED_PLAY_LOCATIONS = (GUIDES / "decks", DECKLISTS, GUIDES / "ahead-of-pin" / "decks")


def test_no_vendored_play_id_disagrees_with_itself_across_locations() -> None:
    """`tests/fixtures/guides/decks`, `tests/driver/fixtures/mtg-decks` and
    `tests/fixtures/guides/ahead-of-pin/decks` each vendor their own copy of
    (a subset of) the same mtg-decks corpus. A `(slug, play id)` pair that
    exists in more than one of them is supposed to be the SAME play, byte for
    byte - this regresses the exact defect a stale `ahead-of-pin` copy caused
    once the pin refresh caught up to it: the vendored slice and
    `ahead-of-pin` both carried `boros-aggro/open-fetch-unlocks` with
    different bytes (a Marsh Flats regression fix landed in the newer one),
    and nothing noticed."""
    seen: dict[tuple[str, str], list[tuple[str, bytes]]] = {}
    for root in VENDORED_PLAY_LOCATIONS:
        for path in sorted(root.glob("*/plays/*.json")):
            if path.name == "inputs.json":
                continue
            slug = path.parent.parent.name
            doc = json.loads(path.read_text())
            script_id = doc["id"]
            seen.setdefault((slug, script_id), []).append((str(path), path.read_bytes()))

    disagreements = [
        (key, [loc for loc, _ in copies])
        for key, copies in seen.items()
        if len(copies) > 1 and len({data for _, data in copies}) > 1
    ]
    assert not disagreements, (
        "the same (slug, play id) vendored with different bytes in more than one "
        f"location: {disagreements}"
    )


# -- the MCP surface ----------------------------------------------------------------


def mcp_server(client: TestClient, monkeypatch: pytest.MonkeyPatch, game_id: str) -> Any:
    seats, _ = wired(client, monkeypatch, game_id)
    return build_server(seats)


def run(server: Any, uid: str, script: str, **extra: Any) -> dict[str, Any]:
    arguments = {"player_uid": uid, "script": script, "reasoning": REASONING, **extra}
    return answer(call_tool(server, "run_script", arguments))


def listing(server: Any, uid: str, **extra: Any) -> dict[str, Any]:
    return answer(call_tool(server, "list_scripts", {"player_uid": uid, **extra}))


def wait(server: Any, uid: str, **extra: Any) -> dict[str, Any]:
    """A real `wait_for_priority` wake, the normal path a live game attaches
    `scripts.applicable`/`scripts.advice` (`mcp._attach_script_hint`) through,
    rather than calling `script_tools.applicable_hint` directly."""
    return answer(call_tool(server, "wait_for_priority", {"player_uid": uid, **extra}))


def log_rows(client: TestClient, game_id: str, uid: str) -> list[dict[str, Any]]:
    """The real `GET .../log` route's own rows for `uid` - the seat-filtered
    read a live player gets, never `audit.entries_since` read straight off
    the process."""
    response = client.get(f"/api/games/{game_id}/log", params={"uid": uid})
    assert response.status_code == 200, response.text
    return response.json()["log"]


# -- the board ----------------------------------------------------------------------


def named(cards: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [card for card in cards if card["name"] == name]


def printed_text(name: str) -> str:
    raw = scryfall.load_cached(name) or scryfall.load_pooled(name)
    assert raw is not None, f"no local printing of {name}"
    return str((raw.get("card_faces") or [raw])[0].get("oracle_text") or raw["oracle_text"])


def declare_plan(client: TestClient, game_id: str, player: str) -> None:
    body = {"player": player, "uid": seat_uid(game_id, player), **PLAN}
    response = client.post(f"/api/games/{game_id}/plan", json=body)
    assert response.status_code == 200, response.text


def set_life(game_id: str, player: str, life: int) -> None:
    game = store.get(game_id)
    assert game is not None
    game.player(player).life = life


def hand_priority_to_me(client: TestClient, game_id: str) -> None:
    """On the opponent's own turn the opponent holds priority first; one real
    pass on an empty stack hands it on without ending the step, which takes
    every player passing in succession (CR 117.4)."""
    uid = seat_uid(game_id, OPPONENT)
    passed = client.post(
        f"/api/games/{game_id}/actions/pass-priority",
        params={"uid": uid},
        json={"player": OPPONENT},
    )
    assert passed.status_code == 200, passed.text
    assert state(client, game_id)["priority_holder"] == ME


def fetch_shock_board(client: TestClient, game_id: str) -> None:
    """Alice's precombat main, stack empty, plan declared: Arid Mesa and a
    second land (Plains, so it never satisfies "an untapped Mountain") so the
    play's own "a second land already down" true condition holds; a red
    Galvanic Discharge in hand for its "a red card is in hand" true
    condition, turn 3 (`own_turns_taken`, CR 500.7) so its "not turn 1-2"
    false condition does not trip, and no untapped Mountain, so the
    refreshed play's red is the colour that is missing; Sacred Foundry and
    six more Galvanic Discharge in the library, so the library is past the
    play's size guard and the shock is the one find."""
    force_turn_position(game_id, Step.PRECOMBAT_MAIN, ME)
    game = store.get(game_id)
    assert game is not None and game.turn is not None
    game.turn.turns_taken_by[ME] = 3
    add_card(client, game_id, "Arid Mesa", ME, "battlefield")
    add_card(client, game_id, "Plains", ME, "battlefield")
    add_card(client, game_id, "Galvanic Discharge", ME, "hand")
    add_card(client, game_id, "Sacred Foundry", ME, "library")
    for _ in range(6):
        add_card(client, game_id, "Galvanic Discharge", ME, "library")
    declare_plan(client, game_id, ME)


# -- open-fetch-unlocks (M2: a search's own pick needs a library land's unlocks) --


def open_fetch_unlocks_board(client: TestClient, game_id: str) -> None:
    """Turn 1, precombat main, land drop available: Arid Mesa ("Search your
    library for a Mountain or Plains card"), Ragavan Nimble Pilferer (needs
    R) and Guide of Souls (needs W, never cast this test) in hand; the
    library holds every legal find that clause can name - Mountain, Sacred
    Foundry and Elegant Parlor (both "Land - Mountain Plains"), and Plains -
    plus filler past the play's own `zone_size <= 5` stand-down guard."""
    force_turn_position(game_id, Step.PRECOMBAT_MAIN, ME)
    add_card(client, game_id, "Arid Mesa", ME, "hand")
    add_card(client, game_id, "Ragavan, Nimble Pilferer", ME, "hand")
    add_card(client, game_id, "Guide of Souls", ME, "hand")
    add_card(client, game_id, "Mountain", ME, "library")
    add_card(client, game_id, "Sacred Foundry", ME, "library")
    add_card(client, game_id, "Elegant Parlor", ME, "library")
    add_card(client, game_id, "Plains", ME, "library")
    for _ in range(3):
        add_card(client, game_id, "Lightning Bolt", ME, "library")
    declare_plan(client, game_id, ME)


def test_open_fetch_unlocks_picks_a_library_land_that_actually_unlocks_a_hand_card(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect this regresses: `hypothetical_facts.build_hypothetical_facts`
    used to compute `enters`/`unlocks` for hand lands only, so this play's own
    second move - `activate` Arid Mesa, answered by a `choose_cards` pick
    naming `select: {types_all: [Land], unlocks: true}` - could never match a
    library candidate, and the run exited `unforeseen` right after the fetch
    was sacrificed and its life paid. Fixed, the pick reads Mountain's own
    `unlocks` (Ragavan needs R and nothing else in this board pays it, so
    Mountain is the only land besides Plains that unlocks anything, and the
    pick's own `prefer: [{supertypes_any: [Basic]}, ...]` plus O2's "ties any"
    alphabetical tiebreak both land on Mountain regardless) and the run
    completes both moves."""
    open_fetch_unlocks = play_of("boros-aggro", "open-fetch-unlocks")
    alice = seat_uid(game, ME)
    register_corpus(client, game, alice, "boros-aggro")
    open_fetch_unlocks_board(client, game)
    server = mcp_server(client, monkeypatch, game)
    before = state(client, game)

    with opponent_passes(game):
        payload = run(server, alice, open_fetch_unlocks["id"])

    assert payload["script_exit"]["exit"] == "chain_complete", payload["script_exit"]
    assert (
        payload["script_exit"]["moves_done"]
        == payload["script_exit"]["moves_total"]
        == len(open_fetch_unlocks["moves"])
        == 2
    ), payload["script_exit"]
    after = state(client, game)
    assert named(after["graveyards"][ME], "Arid Mesa"), "the fetch was sacrificed"
    [mountain] = named(after["battlefield"], "Mountain")
    assert mountain["controller"] == ME
    assert not named(after["battlefield"], "Sacred Foundry"), "Mountain, not the dual, was picked"
    assert not named(after["battlefield"], "Elegant Parlor")
    assert not named(after["battlefield"], "Plains")
    assert life_of(after, ME) == life_of(before, ME) - 1, "Arid Mesa's own Pay 1 life, nothing else"


# -- battlefield unlocks_by_search (M2: choose among several already-in-play fetches) --

#: dimir-frog, esper-blink and instant-reanimator's real `open-fetch-unlocks`
#: all share this exact `activate.source.select` shape - `unlocks_by_search`
#: named on a BATTLEFIELD land, to choose among several already-in-play,
#: untapped fetches rather than crack whichever one is merely untapped. Built
#: here rather than read off a sibling worktree: the real dimir-frog file (at
#: `mtg-decks-w1/decks/dimir-frog/plays/open-fetch-unlocks.json`, read once to
#: copy this shape faithfully) lives in another agent's temporary, numbered
#: scratch worktree - a fine thing to read, never a fine thing for a landed
#: test to depend on, since that worktree does not outlive its own task. This
#: is that same file's exact `true`/`false`/`moves` shape, `id` renamed.
DIMIR_FROG_BATTLEFIELD_FETCH_PICK = json.loads(
    """
    {
      "schema_version": 1,
      "id": "open-fetch-unlocks-battlefield-choice",
      "name": "Crack the untapped fetch that can actually reach the unlock",
      "priority": 72,
      "opposing_archetypes": ["*"],
      "when": {"whose_turn": "mine", "steps": ["precombat_main"], "stack": "empty"},
      "true": [
        {"kind": "turn", "op": "<=", "n": 2},
        {"kind": "land_drop_available", "value": true},
        {
          "kind": "count", "zone": "hand", "side": "me", "op": ">=", "n": 1,
          "select": {
            "types_all": ["Land"],
            "searches_for_any": ["Plains", "Island", "Swamp", "Mountain", "Forest"],
            "unlocks_by_search": true
          }
        }
      ],
      "false": [
        {
          "kind": "count", "zone": "hand", "side": "me", "op": ">=", "n": 1,
          "select": {"types_all": ["Land"], "unlocks": true, "enters_any": ["untapped"]}
        },
        {"kind": "life", "side": "me", "op": "<=", "n": 7},
        {"kind": "zone_size", "zone": "library", "side": "me", "op": "<=", "n": 5}
      ],
      "moves": [
        {
          "kind": "play_land",
          "card": {
            "select": {
              "types_all": ["Land"],
              "searches_for_any": ["Plains", "Island", "Swamp", "Mountain", "Forest"],
              "unlocks_by_search": true
            },
            "ties": "any"
          }
        },
        {
          "kind": "activate",
          "source": {
            "zone": "battlefield",
            "select": {
              "types_all": ["Land"],
              "searches_for_any": ["Plains", "Island", "Swamp", "Mountain", "Forest"],
              "tapped": false,
              "unlocks_by_search": true
            },
            "ties": "any"
          },
          "ability_text_has": "Search your library for",
          "sacrifice": "source",
          "answers": [
            {
              "decision": "choose_cards",
              "pick": {
                "select": {"types_all": ["Land"], "unlocks": true},
                "count": 1,
                "prefer": [{"supertypes_any": ["Basic"]}],
                "ties": "any"
              }
            },
            {"decision": "pay_life", "accept": true}
          ]
        }
      ],
      "next": []
    }
    """
)


def dimir_frog_battlefield_choice_board(client: TestClient, game_id: str) -> None:
    """Turn 1, precombat main, land drop available: Polluted Delta ("Search
    your library for an Island or Swamp card") and Thoughtseize (needs B) in
    hand; Flooded Strand ("...for a Plains or Island card") already on the
    battlefield, untapped, but unable to reach anything that unlocks
    Thoughtseize - proving the activate move's own `unlocks_by_search`
    selector picks the fetch that actually can, not merely the first
    untapped one. Library holds the one land that does (a Swamp) plus filler
    past the play's own `zone_size <= 5` stand-down guard."""
    force_turn_position(game_id, Step.PRECOMBAT_MAIN, ME)
    add_card(client, game_id, "Flooded Strand", ME, "battlefield")
    add_card(client, game_id, "Polluted Delta", ME, "hand")
    add_card(client, game_id, "Thoughtseize", ME, "hand")
    add_card(client, game_id, "Swamp", ME, "library")
    for _ in range(5):
        add_card(client, game_id, "Psychic Frog", ME, "library")
    declare_plan(client, game_id, ME)


def test_battlefield_fetch_selects_the_untapped_one_that_actually_unlocks(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect this regresses (M2): `build_hypothetical_facts` used to
    compute `unlocks_by_search` for hand cards only, so a play whose
    `activate` move picks AMONG SEVERAL already-in-play, untapped fetches by
    `source.select`'s own `unlocks_by_search: true` (dimir-frog, esper-blink
    and instant-reanimator's real `open-fetch-unlocks` all share this exact
    shape) could never match a battlefield land at all - the source select
    found nothing and the run exited `unforeseen`. Fixed, Flooded Strand
    (cannot reach a black source) is correctly passed over for Polluted
    Delta (can), which is cracked for the library's own Swamp."""
    alice = seat_uid(game, ME)
    register_corpus(client, game, alice, "dimir-frog", DIMIR_FROG_BATTLEFIELD_FETCH_PICK)
    dimir_frog_battlefield_choice_board(client, game)
    server = mcp_server(client, monkeypatch, game)
    before = state(client, game)

    with opponent_passes(game):
        payload = run(server, alice, DIMIR_FROG_BATTLEFIELD_FETCH_PICK["id"])

    assert payload["script_exit"]["exit"] == "chain_complete", payload["script_exit"]
    assert payload["script_exit"]["moves_done"] == payload["script_exit"]["moves_total"] == 2, (
        payload["script_exit"]
    )
    after = state(client, game)
    assert named(after["graveyards"][ME], "Polluted Delta"), "the reaching fetch was cracked"
    assert named(after["battlefield"], "Flooded Strand"), "the non-reaching fetch stayed put"
    [swamp] = named(after["battlefield"], "Swamp")
    assert swamp["controller"] == ME
    assert life_of(after, ME) == life_of(before, ME) - 1, (
        "Polluted Delta's own Pay 1 life, nothing else"
    )


@contextmanager
def opponent_responds_with_bolt(game_id: str) -> Iterator[list[dict[str, Any]]]:
    """The opponent, the first time it holds priority over an object of
    mine, casts its Lightning Bolt at me instead of passing."""
    uid = seat_uid(game_id, OPPONENT)
    other = TestClient(app)
    casts: list[dict[str, Any]] = []
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set() and not casts:
            board = other.get(f"/api/games/{game_id}", params={"uid": uid}).json()
            stack = board.get("stack") or []
            if (
                board.get("priority_holder") == OPPONENT
                and board.get("pending_decision") is None
                and stack
                and stack[0].get("controller") == ME
            ):
                bolt = named(board["hands"][OPPONENT], "Lightning Bolt")[0]
                mountain = [
                    card
                    for card in named(board["battlefield"], "Mountain")
                    if card["controller"] == OPPONENT
                ][0]
                cast = other.post(
                    f"/api/games/{game_id}/actions/cast",
                    params={"uid": uid},
                    json={
                        "player": OPPONENT,
                        "card_id": bolt["id"],
                        "targets": [ME],
                        "mana_sources": [mountain["id"]],
                    },
                )
                casts.append({"status": cast.status_code, **cast.json()})
            stop.wait(0.02)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        yield casts
    finally:
        stop.set()
        thread.join(5)


def decline_opponent_yes_no(client: TestClient, game_id: str, player: str = OPPONENT) -> bool:
    """If `player` has a pending `may`/`pay_life`/`pay_energy` decision (CR
    608.2d, CR 119.4, CR 118.3) - a Tron piece's destruction handing its own
    controller their own "you may search for a basic land", say - decline
    it and return True; otherwise do nothing and return False. A one-shot
    call between `run_script` attempts, never raced against one while it is
    active: `runtime._foreign` (B7-09) treats ANY action from another seat,
    even that seat legitimately answering its own decision, as this run
    being interrupted - so the caller's own loop must let `run_script`
    return (`exit: null`, paused on someone else's decision) first, answer
    the decision here, then resume."""
    uid = seat_uid(game_id, player)
    pending = state(client, game_id, uid).get("pending_decision")
    if not (
        pending is not None
        and pending.get("player") == player
        and pending.get("kind") in ("may", "pay_life", "pay_energy")
    ):
        return False
    response = client.post(
        f"/api/games/{game_id}/decide",
        params={"uid": uid},
        json={"player": player, "accept": False},
    )
    assert response.status_code == 200, response.text
    return True


# -- fetch-shock ------------------------------------------------------------------------


def test_the_fetch_shock_seed_cracks_arid_mesa_for_an_untapped_sacred_foundry(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(game, ME)
    play = play_of("boros-aggro", "fetch-untapped-shock")
    register_corpus(client, game, alice, "boros-aggro")
    fetch_shock_board(client, game)
    server = mcp_server(client, monkeypatch, game)
    fetch_life = int(re.search(r"Pay (\d+) life", printed_text("Arid Mesa")).group(1))
    shock_life = int(re.search(r"pay (\d+) life", printed_text("Sacred Foundry")).group(1))
    before = state(client, game)
    watermark = audit.last_seq(game)

    with opponent_passes(game):
        payload = run(server, alice, play["id"])

    assert payload["script_exit"] == {
        "exit": "chain_complete",
        "script": play["id"],
        "moves_done": 1,
        "moves_total": 1,
    }, payload["script_exit"]
    after = state(client, game)
    assert named(after["graveyards"][ME], "Arid Mesa")
    [foundry] = named(after["battlefield"], "Sacred Foundry")
    assert foundry["tapped"] is False, "the shock's 2 life was paid, so it entered untapped"
    assert life_of(after, ME) == life_of(before, ME) - fetch_life - shock_life

    rows = audit.entries_since(game, watermark)
    activation = next(
        index
        for index, row in enumerate(rows)
        if row["action"] == "activate" and row["detail"].get("player") == ME
    )
    resolved = next(
        index for index in range(activation, len(rows)) if rows[index]["stack_size"] == 0
    )
    # CR 117.4: the ability resolves only once both players pass in succession,
    # so bob held priority over it, and passed, before it resolved.
    assert any(
        row["action"] == "pass_priority"
        and row["detail"].get("player") == OPPONENT
        and row["stack_size"] >= 1
        for row in rows[activation + 1 : resolved]
    ), [(row["action"], row["detail"].get("player"), row["stack_size"]) for row in rows]

    suffix = f"script {play['id']}@{runtime.script_sha(play)[:8]} move 1/1 (activate)"
    moves = [
        row
        for row in rows
        if row["detail"].get("player") == ME
        and row["action"] in ("activate", "pass_priority", "decide")
    ]
    assert moves and all(
        row["reasoning"] == f"{REASONING} | {suffix}" and row["reasoning"].endswith(suffix)
        for row in moves
    ), [row["reasoning"] for row in moves]
    [record] = finished(game)
    assert record["moves_done"] == 1 and record["exit"] == "chain_complete"


# -- API-10: an offer row reaches the ledger and pairs up with the run -----------------------


def register_two_scripts(
    client: TestClient,
    game_id: str,
    uid: str,
    move_play: dict[str, Any],
    advice_play_doc: dict[str, Any],
) -> None:
    """Exactly `[move_play, advice_play_doc]` as `uid`'s whole corpus - never
    the full real boros-aggro corpus (`register_corpus`), so nothing else
    registered can also apply on this board and blur which offer is which."""
    body = {
        "deck_slug": "boros-aggro",
        "guides_revision": GUIDES_REVISION,
        "scripts": [move_play, advice_play_doc],
        "inputs": [],
    }
    response = client.put(
        f"/api/games/{game_id}/seats/{uid}/scripts", params={"uid": uid}, json=body
    )
    assert response.status_code == 200, response.text


def test_an_offered_move_bearing_and_advice_only_play_both_reach_the_ledger_and_pair_up(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API-10: the whole path, end to end, rather than any one piece of it
    mocked out - every piece here (the hint, the offer post, the log's own
    seat filtering, the run itself, `offer_table`) already has its own unit
    tests; this is the seam none of them individually prove.

    The advice-only sibling is the real `fetch-untapped-shock` play itself,
    `moves`/`next` emptied (`advice_play`) - same `when`/`true`/`false`, so
    it applies on exactly the same board the move-bearing play does, at the
    same turn and step, in the same offer set."""
    alice = seat_uid(game, ME)
    bob = seat_uid(game, OPPONENT)
    move_play = play_of("boros-aggro", "fetch-untapped-shock")
    advice_play_doc = advice_play(
        move_play, id="fetch-untapped-shock-advice-note", name="Note: the same read, nothing to run"
    )
    register_two_scripts(client, game, alice, move_play, advice_play_doc)
    fetch_shock_board(client, game)
    server = mcp_server(client, monkeypatch, game)

    # 2: a real wake, the normal path a live game attaches the hint through.
    woken = wait(server, alice)

    # 3: scripts.applicable/scripts.advice, `why` and `offer_seq` on each.
    scripts = woken.get("scripts")
    assert scripts is not None, woken
    [applicable] = [e for e in scripts.get("applicable", []) if e["id"] == move_play["id"]]
    assert applicable["why"], applicable
    assert isinstance(applicable["offer_seq"], int), applicable
    [advice] = [e for e in scripts.get("advice", []) if e["id"] == advice_play_doc["id"]]
    assert advice["why"], advice
    assert isinstance(advice["offer_seq"], int), advice

    # 4: one script_run_offered row per offered play, actually in the
    # ledger, readable back through the log route by alice, at the exact
    # offer_seq the answer named.
    alice_log = log_rows(client, game, alice)
    offered_by_seq = {
        row["seq"]: row for row in alice_log if row["action"] == audit.SCRIPT_RUN_OFFERED_ACTION
    }
    assert offered_by_seq[applicable["offer_seq"]]["detail"]["script_id"] == move_play["id"], (
        alice_log
    )
    assert offered_by_seq[advice["offer_seq"]]["detail"]["script_id"] == advice_play_doc["id"], (
        alice_log
    )

    # 5: bob's own read of the same log carries neither row.
    bob_log = log_rows(client, game, bob)
    assert not [row for row in bob_log if row["action"] == audit.SCRIPT_RUN_OFFERED_ACTION], (
        bob_log
    )

    # 7: repeat the same wake at the same turn and step (nothing on the
    # board moved) - the F4 dedupe: no second offer row, the same offer_seq
    # both times.
    watermark = audit.last_seq(game)
    woken_again = wait(server, alice)
    scripts_again = woken_again["scripts"]
    [applicable_again] = [
        e for e in scripts_again.get("applicable", []) if e["id"] == move_play["id"]
    ]
    [advice_again] = [
        e for e in scripts_again.get("advice", []) if e["id"] == advice_play_doc["id"]
    ]
    assert applicable_again["offer_seq"] == applicable["offer_seq"], (applicable, applicable_again)
    assert advice_again["offer_seq"] == advice["offer_seq"], (advice, advice_again)
    assert audit.last_seq(game) == watermark, "the repeat wake posted a second offer row"

    # 6: run the offered play - never refused `already_ran` (F2, end to end,
    # not only at the unit level: an offer never holds the once-per-turn-
    # and-step slot a start does).
    with opponent_passes(game):
        out = run(server, alice, move_play["id"])["script_exit"]
    assert out["exit"] != "already_ran", out
    assert out["exit"] == "chain_complete", out

    # `offer_table` over the resulting ledger: the move-bearing play taken,
    # the advice-only sibling passed over (its own offer set's only other
    # member was the one actually taken) rather than declined.
    table = report.offer_table(audit.entries_since(game, 0), game, ME)
    move_line = next(line for line in table.splitlines() if line.startswith(f"{move_play['id']} |"))
    advice_line = next(
        line for line in table.splitlines() if line.startswith(f"{advice_play_doc['id']} |")
    )
    assert move_line.split(" | ")[5] == "taken", table
    assert advice_line.split(" | ")[5] == "passed over", table


# -- fetch-surveil ------------------------------------------------------------------------


def test_the_fetch_surveil_seed_fetches_a_tapped_undercity_sewers_at_bobs_end_step(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five lands out, so the play bins lands; every card left in the library
    is a land, so whatever the shuffle leaves on top is one `to_graveyard`
    names."""
    alice = seat_uid(game, ME)
    play = play_of("dimir-frog", "fetch-surveil-land")
    register_corpus(client, game, alice, "dimir-frog")
    force_turn_position(game, Step("end_step"), OPPONENT)
    for name in ("Polluted Delta", "Island", "Island", "Swamp", "Watery Grave"):
        add_card(client, game, name, ME, "battlefield")
    add_card(client, game, "Undercity Sewers", ME, "library")
    for _ in range(6):
        add_card(client, game, "Island", ME, "library")
    hand_priority_to_me(client, game)
    server = mcp_server(client, monkeypatch, game)
    watermark = audit.last_seq(game)

    with opponent_passes(game):
        payload = run(server, alice, play["id"])

    assert payload["script_exit"]["exit"] == "chain_complete", payload["script_exit"]
    after = state(client, game)
    [sewers] = named(after["battlefield"], "Undercity Sewers")
    assert sewers["tapped"] is True, "Undercity Sewers enters tapped"
    graveyard = after["graveyards"][ME]
    assert named(graveyard, "Polluted Delta")
    assert len(named(graveyard, "Island")) == 1, "the surveilled land went to the graveyard"
    me = next(player for player in after["players"] if player["username"] == ME)
    assert me["library_count"] == 5
    decisions = [
        row
        for row in audit.entries_since(game, watermark)
        if row["action"] == "decide" and row["detail"].get("player") == ME
    ]
    assert len(decisions) >= 2, "the search and the surveil trigger were both answered"


# -- interrupted, already_ran, advised, not_applicable ------------------------------------


def test_bob_responding_to_the_fetch_interrupts_the_run_and_nothing_more_is_sent(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(game, ME)
    play = play_of("boros-aggro", "fetch-untapped-shock")
    register_corpus(client, game, alice, "boros-aggro")
    fetch_shock_board(client, game)
    add_card(client, game, "Mountain", OPPONENT, "battlefield")
    add_card(client, game, "Lightning Bolt", OPPONENT, "hand")
    server = mcp_server(client, monkeypatch, game)
    watermark = audit.last_seq(game)

    with opponent_responds_with_bolt(game) as casts:
        out = run(server, alice, play["id"])["script_exit"]

    assert [cast["status"] for cast in casts] == [200] and casts[0]["allowed"] is True, casts
    assert out["exit"] == "interrupted", out
    # B7-14: the fetch was accepted before the Bolt interrupted it, so it counts.
    assert out["moves_done"] == 1 == out["moves_total"], out
    assert out["stop_reason"].startswith(f"{OPPONENT} acted (cast"), out
    stack = state(client, game)["stack"]
    assert [obj["controller"] for obj in stack] == [OPPONENT, ME], "the bolt sits unanswered"
    rows = audit.entries_since(game, watermark)
    cast_at = next(
        index
        for index, row in enumerate(rows)
        if row["action"] == "cast" and row["detail"].get("player") == OPPONENT
    )
    assert [
        row["action"] for row in rows[cast_at + 1 :] if row["action"] != audit.SCRIPT_RUN_ACTION
    ] == [], "the run issued nothing after the response"


def test_a_second_run_of_the_seed_in_the_same_turn_and_step_is_already_ran(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(game, ME)
    play = play_of("boros-aggro", "fetch-untapped-shock")
    register_corpus(client, game, alice, "boros-aggro")
    fetch_shock_board(client, game)
    server = mcp_server(client, monkeypatch, game)

    with opponent_passes(game):
        first = run(server, alice, play["id"])["script_exit"]
        second = run(server, alice, play["id"])["script_exit"]

    assert first["exit"] == "chain_complete", first
    assert second["exit"] == "already_ran", second
    assert second["prior_run_id"] == finished(game)[0]["run_id"]


def test_the_advisor_running_the_seed_is_advised_and_the_board_does_not_move(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    advisor = "advisor-uid"
    store.register_advisors(game, {advisor: Advisor(player=ME)})
    play = play_of("boros-aggro", "fetch-untapped-shock")
    register_corpus(client, game, advisor, "boros-aggro")
    fetch_shock_board(client, game)
    server = mcp_server(client, monkeypatch, game)
    before = state(client, game)

    out = run(server, advisor, play["id"])["script_exit"]

    assert out["exit"] == "advised", out
    assert out["proposal"]["proposed"] is True
    after = state(client, game)
    for section in ("battlefield", "stack", "graveyards", "players"):
        assert after[section] == before[section], section


def test_life_below_the_seeds_threshold_is_not_applicable_naming_that_condition(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(game, ME)
    play = play_of("boros-aggro", "fetch-untapped-shock")
    index, threshold = next(
        (index, condition["n"])
        for index, condition in enumerate(play["true"])
        if condition["kind"] == "life"
    )
    register_corpus(client, game, alice, "boros-aggro")
    fetch_shock_board(client, game)
    set_life(game, ME, threshold - 1)
    server = mcp_server(client, monkeypatch, game)

    out = run(server, alice, play["id"])["script_exit"]

    assert out["exit"] == "not_applicable", out
    assert out["failing_condition"]["list"] == "true"
    assert out["failing_condition"]["index"] == index
    assert out["failing_condition"]["seen"] == threshold - 1
    assert named(state(client, game)["battlefield"], "Arid Mesa")


# -- needs_input and ambiguous --------------------------------------------------------------

PREFER_TEMPO = {
    "name": "prefer_tempo",
    "type": "bool",
    "description": "Whether this game is on the tempo half of the plan.",
    "required": True,
}


def test_a_required_input_the_caller_omits_exits_needs_input_naming_it(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(game, ME)
    seed = play_of("boros-aggro", "fetch-untapped-shock")
    gated = {
        **seed,
        "id": "fetch-untapped-shock-on-tempo",
        "true": [
            *seed["true"],
            {"kind": "input", "name": "prefer_tempo", "op": "eq", "value": True},
        ],
    }
    register_corpus(client, game, alice, "boros-aggro", gated, inputs=[PREFER_TEMPO])
    fetch_shock_board(client, game)
    server = mcp_server(client, monkeypatch, game)

    out = run(server, alice, gated["id"])["script_exit"]

    assert out["exit"] == "needs_input", out
    assert out["needs_input"] == "prefer_tempo"
    assert out["failing_condition"]["list"] == "true"
    assert out["failing_condition"]["index"] == len(seed["true"]), out
    assert named(state(client, game)["battlefield"], "Arid Mesa"), "nothing was cracked"


def test_two_next_plays_tied_at_the_same_decision_point_exit_ambiguous_naming_both(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice = seat_uid(game, ME)
    seed = play_of("boros-aggro", "fetch-untapped-shock")
    twin = {**seed, "id": "fetch-untapped-shock-twin"}
    drop = {
        "schema_version": 1,
        "id": "drop-a-plains",
        "name": "Play a basic Plains, then fetch",
        "priority": 50,
        "opposing_archetypes": ["*"],
        "when": seed["when"],
        "true": [{"kind": "land_drop_available", "value": True}],
        "false": [],
        "moves": [
            {
                "kind": "play_land",
                "card": {
                    "select": {
                        "types_all": ["Land"],
                        "supertypes_any": ["Basic"],
                        "subtypes_any": ["Plains"],
                    }
                },
            }
        ],
        "next": [seed["id"], twin["id"]],
    }
    register_corpus(client, game, alice, "boros-aggro", twin, drop)
    fetch_shock_board(client, game)
    add_card(client, game, "Plains", ME, "hand")
    server = mcp_server(client, monkeypatch, game)

    out = run(server, alice, drop["id"])["script_exit"]

    assert out["exit"] == "ambiguous", out
    assert out["applicable_next"] == [seed["id"], twin["id"]]
    assert out["stop_reason"]
    [mesa] = named(state(client, game)["battlefield"], "Arid Mesa")
    assert mesa["tapped"] is False, "neither tied play was taken"


# -- staging a board for any play -----------------------------------------------------

#: Past every library-size guard in the slice, and enough for a draw.
LIBRARY_FLOOR = 7
ZONES = ("battlefield", "hand", "library", "graveyard")
SEARCH_CLAUSE = re.compile(r"search your library for (?:an? |up to \w+ )?(.+?) cards?\b", re.I)
MANA_LINE = re.compile(r"^\{T\}: Add ")
SYMBOL = re.compile(r"\{([^}]+)\}")
COLOURED = frozenset("WUBRGC")
GROWING = (">=", ">", "==")


def printing_view(name: str) -> dict[str, Any] | None:
    """What a selector reads off a card, from its local printing: the front
    face's type line, text, keywords, colours and mana value, and each line
    of text holding a colon as an ability. None for a name with no local
    printing, which the stager then never picks.

    A modal double-faced card (`layout: "modal_dfc"`, CR 712.12) also gets a
    `faces` list, `serialize._faces_view`'s own shape - each face's name,
    mana cost, type line and oracle text, the one whose type line names
    "Land" (`magic_cards_model.models.card.Card.land_face`) marked
    `land_face: True`. `selectors._has_land_face` (O5) is what actually reads
    it: without this, EVERY candidate the stager builds is read as a
    single-faced card ("a plain land is its own land face"), so a play
    asking for `land_face: true` could only ever be satisfied by a genuine
    Land - which then trips the very guard ("zero plain lands in hand") a
    play like `open-land-idle-mdfc` pairs it with, since the same card would
    have to be both. A transform or meld card (CR 712.2) also carries
    `card_faces` but never gets a `faces` list here: its land back, if any,
    is reached only by transforming or melding, never a land drop, so
    `land_face` never marks it - matching `Card.land_face`'s own `layout is
    not MODAL_DFC` refusal exactly."""
    raw = scryfall.load_cached(name) or scryfall.load_pooled(name)
    if raw is None:
        return None
    card_faces = raw.get("card_faces") or []
    face = (card_faces or [raw])[0]
    text = str(face.get("oracle_text") or raw.get("oracle_text") or "")
    lines = [line.strip().strip("()") for line in text.splitlines()]
    view = {
        "name": raw["name"],
        "type_line": str(face.get("type_line") or raw.get("type_line") or ""),
        "oracle_text": text,
        "mana_cost": str(face.get("mana_cost") or raw.get("mana_cost") or ""),
        "keywords": list(raw.get("keywords") or []),
        "colors": list(face.get("colors") or raw.get("colors") or []),
        "mana_value": float(raw.get("cmc") or 0),
        "tapped": False,
        "counters": {},
        "abilities": [{"text": line} for line in lines if ":" in line],
    }
    if raw.get("layout") == "modal_dfc" and len(card_faces) > 1:
        land_face = next(
            (
                f
                for f in card_faces
                if "land" in str(f.get("type_line", "")).split("—")[0].lower().split()
            ),
            None,
        )
        view["faces"] = [
            {
                "name": f.get("name"),
                "mana_cost": f.get("mana_cost"),
                "type_line": f.get("type_line"),
                "oracle_text": f.get("oracle_text"),
                **({"land_face": True} if f is land_face else {}),
            }
            for f in card_faces
        ]
    return view


def deck_pool(slug: str) -> list[dict[str, Any]]:
    """Every card the deck's 75 names, in list order, as printing views."""
    decklist = parse_decklist((DECKLISTS / slug / "decklist.txt").read_text())
    names = dict.fromkeys([*decklist.mainboard, *decklist.sideboard])
    return [view for view in map(printing_view, names) if view is not None]


@functools.cache
def universal_pool() -> tuple[dict[str, Any], ...]:
    """Every card this repo has real local printing data for - the engine's
    own bundled corpus (`magic_cards_model`'s `data/printings/*.json`, ~1000
    real printings) plus this suite's own `tests/fixtures/scryfall/` cache -
    as printing views. Used only for staging an OPPONENT's battlefield
    (`stage_opponent_count`): a play's own true conditions never say what
    deck the opponent runs, only what a card of theirs looks like, so any
    real card matching that is a legitimate stand-in for one out of their
    (unmodeled) 75. Cached: this repo's local corpus does not change between
    calls within one test run."""
    import magic_cards_model

    names: set[str] = set()
    for directory in (
        Path(magic_cards_model.__file__).resolve().parent / "data" / "printings",
        scryfall.cache_dir(),
    ):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            try:
                raw = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            name = raw.get("name") if isinstance(raw, dict) else None
            if isinstance(name, str):
                names.add(name)
    views = (printing_view(name) for name in names)
    return tuple(view for view in views if view is not None)


def stage_opponent_count(board: Board, condition: dict[str, Any]) -> None:
    """Satisfy a `true` count `condition` naming `side: "opponent"`: find a
    real card in `universal_pool()` matching its (never hypo-only - the
    corpus never names one on an opponent condition) selector and add it to
    `board.opponent_cards`. No `breaks()`/false-guard check: no play in the
    corpus reads the opponent's side in a `false` condition, so there is
    nothing yet to avoid tripping."""
    present = board_present(board)
    zone = condition["zone"]
    select = condition["select"]
    fit = next((view for view in universal_pool() if loose_matches(select, view, present)), None)
    if fit is None:
        pytest.fail(f"no locally known card can be the opponent's own {condition}")
    board.opponent_cards.append((zone, fit))


@dataclass
class Board:
    """A plan for my side of the board: cards by zone, my life, and my own
    `turn` (`TurnMachine.own_turns_taken` - CR 500.7 - 1 during my own first
    turn, the state a freshly created game's active player already carries,
    hence the default)."""

    cards: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    life: int = 20
    turn: int = 1
    #: The `opposing_archetype` a `run_script`/`list_scripts` call would be
    #: given (`mcp.py`'s own `str = "*"` default - never unset, unlike a
    #: plain declared input) and every other named input (`kind: "input"`)
    #: `stage`'s own true-condition pass had to pin a value for.
    opposing_archetype: str = "*"
    inputs: dict[str, Any] = field(default_factory=dict)
    #: The opponent's own side, staged only when a `count` condition names
    #: `side: "opponent"` (`stage_opponent_count`) - empty otherwise, which is
    #: exactly what `board_present`'s own long-standing "the opponent's side
    #: is always staged empty" already promised for every play that never
    #: reads one.
    opponent_cards: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def zone(self, zone: str) -> list[dict[str, Any]]:
        return [view for where, view in self.cards if where == zone]

    def opponent_zone(self, zone: str) -> list[dict[str, Any]]:
        return [view for where, view in self.opponent_cards if where == zone]

    def plus(self, zone: str, view: dict[str, Any]) -> Board:
        return Board(
            [*self.cards, (zone, view)],
            self.life,
            self.turn,
            self.opposing_archetype,
            self.inputs,
            self.opponent_cards,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "life": self.life,
            **{z: [v["name"] for v in self.zone(z)] for z in ZONES},
            "opponent": {z: [v["name"] for v in self.opponent_zone(z)] for z in ZONES},
        }


def board_present(board: Board) -> Callable[[Sequence[str], str], list[dict[str, Any]]]:
    """`matches`'s `present`, read off the board being staged: the opponent's
    side is staged empty unless a `count` condition named `side: "opponent"`
    (`stage_opponent_count`), in which case `board.opponent_cards` carries
    whatever that staged."""

    def present(zones: Sequence[str], side: str) -> list[dict[str, Any]]:
        found = []
        if side in ("me", "any"):
            found += [view for zone in zones for view in board.zone(zone)]
        if side in ("opponent", "any"):
            found += [view for zone in zones for view in board.opponent_zone(zone)]
        return found

    return present


def card_types(view: dict[str, Any]) -> set[str]:
    return parse_type_line(view["type_line"])[1]


def mana_sources(board: Board) -> list[dict[str, Any]]:
    return [
        view
        for view in board.zone("battlefield")
        if not view["tapped"] and any(MANA_LINE.match(a["text"]) for a in view["abilities"])
    ]


def produces(view: dict[str, Any], symbol: str) -> bool:
    return any(
        MANA_LINE.match(a["text"]) and f"{{{symbol}}}" in a["text"] for a in view["abilities"]
    )


#: Selector fields only the live engine's own `hypothetical_facts.py` can
#: answer (`selectors._HYPO_FIELDS`, mirrored here rather than imported since
#: it is a private name). Staging builds a board offline, with no `Game` to
#: ask `enters_any`/`unlocks`/`unlocks_by_search`/`unlocks_by_basic_search`/
#: `affordable` about, so every use below reads one of two ways: `holds`
#: (and `breaks`, which is built on it) treats a selector naming one as
#: never matching - the safe reading for a guard, since it means the guard
#: is never judged tripped by a card staging picked to satisfy something
#: else, and the real board's own `judged["applies"]`, downstream in the
#: actual test, is what actually proves a false condition still does not
#: trip. `loose_matches` instead drops the field and judges the selector on
#: its other characteristics - the reading staging needs wherever a card
#: MUST be found to satisfy a `true` condition or a move's own card/source,
#: since "never matches" would make that impossible to stage at all; the
#: same downstream `judged["applies"]` is what actually proves the card
#: staging picked is the one the live engine agrees does the job.
_HYPO_ONLY_FIELDS = (
    "enters_any",
    "unlocks",
    "unlocks_by_search",
    "unlocks_by_basic_search",
    "affordable",
)


def loose_matches(
    selector: dict[str, Any],
    card: dict[str, Any],
    present: Callable[[Sequence[str], str], list[dict[str, Any]]],
) -> bool:
    """`matches`, with every hypo-only field (see `_HYPO_ONLY_FIELDS`)
    dropped from `selector` first, so staging can still pick a card by a
    selector's other characteristics without asking the live engine."""
    return matches({k: v for k, v in selector.items() if k not in _HYPO_ONLY_FIELDS}, card, present)


def holds(board: Board, condition: dict[str, Any]) -> bool:
    """Whether `condition` holds on the plan. The opponent's side is staged
    empty and at 20 life; the stack is staged empty. A `count` selector
    naming a hypo-only field (see `_HYPO_ONLY_FIELDS`) never matches here -
    `holds_loose` is the `true`-condition-staging reading that drops it
    instead."""
    kind = condition["kind"]
    side = condition.get("side", "me")
    mine = side in ("me", "any")
    if kind == "count":
        cards = []
        if side in ("me", "any"):
            cards += board.zone(condition["zone"])
        if side in ("opponent", "any"):
            cards += board.opponent_zone(condition["zone"])
        found = sum(
            matches(condition["select"], view, board_present(board), hypo={}) for view in cards
        )
        return compare(found, condition["op"], condition["n"])
    if kind == "zone_size":
        size = len(board.zone(condition["zone"])) if mine else 0
        return compare(size, condition["op"], condition["n"])
    if kind == "life":
        return compare(board.life if mine else 20, condition["op"], condition["n"])
    if kind == "untapped_mana":
        return compare(len(mana_sources(board)), condition["op"], condition["n"])
    if kind == "stack_top":
        return False
    if kind == "land_drop_available":
        return condition["value"] is True
    if kind == "turn":
        return compare(board.turn if mine else 0, condition["op"], condition["n"])
    if kind == "input":
        name = condition["name"]
        actual = (
            board.opposing_archetype if name == "opposing_archetype" else board.inputs.get(name)
        )
        return actual is not None and _input_matches(condition["op"], condition["value"], actual)
    pytest.fail(f"staging has no way to decide a {kind} condition: {condition}")


def _input_matches(op: str, expected: Any, actual: Any) -> bool:
    """`conditions._input_holds`'s own `eq`/`ne`/`in`/`not_in` reading,
    mirrored here (the string ops the corpus's own `input` conditions use -
    `opposing_archetype`, `opposing_interaction` - never the numeric ones)."""
    if op == "eq":
        return actual == expected
    if op == "ne":
        return actual != expected
    if op in ("in", "not_in"):
        found = isinstance(expected, list) and actual in expected
        return found if op == "in" else not found
    return False


def holds_loose(board: Board, condition: dict[str, Any]) -> bool:
    """`holds`, but for staging a board to satisfy a `true` condition: a
    `count` selector naming a hypo-only field is judged with that field
    dropped (see `loose_matches`) rather than as never matching, so staging
    can actually find a card for it. `stack_top` reads as always satisfied -
    `Board` has no stack to check (`holds`'s own strict reading is `False`,
    always, for the same reason), so a play whose own true conditions need
    one is trusted to have `stage_opponent_object_on_stack` genuinely put a
    real object there before it runs for real; the live `judged["applies"]`
    downstream is what actually proves it did. Every other condition kind
    reads exactly as `holds` already does."""
    if condition["kind"] == "stack_top":
        return True
    if condition["kind"] != "count":
        return holds(board, condition)
    side = condition.get("side", "me")
    cards = []
    if side in ("me", "any"):
        cards += board.zone(condition["zone"])
    if side in ("opponent", "any"):
        cards += board.opponent_zone(condition["zone"])
    found = sum(loose_matches(condition["select"], view, board_present(board)) for view in cards)
    return compare(found, condition["op"], condition["n"])


def play_selectors(play: dict[str, Any]) -> list[dict[str, Any]]:
    """Every selector the play reads anywhere: its conditions, its moves'
    cards, and its answers' picks, preferences and splits."""
    found = [c["select"] for p in ("true", "false") for c in play[p] if c.get("select")]
    for move in play["moves"]:
        for key in ("source", "card"):
            if isinstance(move.get(key), dict) and move[key].get("select"):
                found.append(move[key]["select"])
        for item in move.get("answers") or []:
            pick = item.get("pick") or {}
            found += [pick["select"]] if pick.get("select") else []
            found += list(pick.get("prefer") or [])
            found += [item[key] for key in ("to_graveyard", "to_bottom") if item.get(key)]
    return found


def finds(searcher: dict[str, Any], target: dict[str, Any]) -> bool:
    """Whether `searcher`'s printed "search your library for ..." clause can
    name `target`, read off the target's own type line; True for a card that
    prints no such clause."""
    clause = SEARCH_CLAUSE.search(searcher["oracle_text"])
    if clause is None:
        return True
    supertypes, types, subtypes = parse_type_line(target["type_line"])
    have = supertypes | types | subtypes
    alternatives = [part for part in re.split(r",| or ", clause.group(1)) if part.strip()]
    return any(set(part.casefold().split()) <= have for part in alternatives)


def breaks(board: Board, play: dict[str, Any], zone: str, view: dict[str, Any]) -> bool:
    after = board.plus(zone, view)
    return any(
        c["kind"] == "count" and holds(after, c) and not holds(board, c) for c in play["false"]
    )


def second_legend(board: Board, zone: str, view: dict[str, Any]) -> bool:
    """A legendary permanent whose name is already on my battlefield: the
    legend rule (CR 704.5j) would ask which to keep before the play runs."""
    supertypes = parse_type_line(view["type_line"])[0]
    return (
        zone == "battlefield"
        and "legendary" in supertypes
        and any(card["name"] == view["name"] for card in board.zone(zone))
    )


def add_chosen(
    board: Board,
    play: dict[str, Any],
    pool: list[dict[str, Any]],
    zone: str,
    wanted: Callable[[dict[str, Any]], bool],
    what: str,
    rank: Callable[[dict[str, Any]], int] | None = None,
) -> None:
    """Put into `zone` the deck's card that is `wanted`, breaks no false
    condition, ranks best, and matches the fewest other selectors the play
    reads - so it does not tie a pick or trip a guard it was not chosen for."""
    selectors = play_selectors(play)
    fits = [
        (index, view)
        for index, view in enumerate(pool)
        if wanted(view)
        and not breaks(board, play, zone, view)
        and not second_legend(board, zone, view)
    ]
    if not fits:
        pytest.fail(f"no card in the deck can be {what} without breaking a false condition")
    _, view = min(
        fits,
        key=lambda item: (
            rank(item[1]) if rank else 0,
            sum(
                matches(selector, item[1], board_present(board), hypo={}) for selector in selectors
            ),
            item[0],
        ),
    )
    board.cards.append((zone, view))


def move_card(move: dict[str, Any], board: Board) -> tuple[str, Callable[[dict[str, Any]], bool]]:
    """The zone a move's card comes from, and what that card must be."""
    present = board_present(board)
    if move["kind"] == "activate":
        spec, phrase = move["source"], move["ability_text_has"].casefold()

        def wanted(view: dict[str, Any]) -> bool:
            return loose_matches(spec["select"], view, present) and any(
                phrase in a["text"].casefold() for a in view["abilities"]
            )

        return spec["zone"], wanted
    spec = move["card"]
    return spec.get("zone", "hand"), lambda view: loose_matches(spec["select"], view, present)


def move_cost(move: dict[str, Any], board: Board) -> list[str]:
    """The move's mana symbols, one entry per mana: a cast card's mana cost,
    or the cost half of the activated ability's own line."""
    if move["kind"] not in ("cast", "activate"):
        return []
    zone, wanted = move_card(move, board)
    card = next(view for view in board.zone(zone) if wanted(view))
    if move["kind"] == "cast":
        text = card["mana_cost"]
    else:
        phrase = move["ability_text_has"].casefold()
        text = next(a["text"] for a in card["abilities"] if phrase in a["text"].casefold())
        text = text.split(":", 1)[0]
    symbols: list[str] = []
    for symbol in SYMBOL.findall(text):
        if symbol.isdigit():
            symbols += ["1"] * int(symbol)
        elif symbol in COLOURED:
            symbols.append(symbol)
    return symbols


def _has_hypo(select: dict[str, Any] | None) -> bool:
    return bool(select) and any(field in select for field in _HYPO_ONLY_FIELDS)


def move_prefer(play: dict[str, Any], zone: str) -> list[dict[str, Any]]:
    """The `prefer` list of the play's own move whose card (or, for an
    `activate`, source) comes from `zone`, empty if none does - staging's
    only source of "which of several fits is the one the play actually
    wants", the same list `run_script`'s own tie-break already reads."""
    for move in play["moves"]:
        card = move.get("card")
        if isinstance(card, dict) and card.get("zone", "hand") == zone:
            return list(card.get("prefer") or [])
        source = move.get("source")
        if isinstance(source, dict) and source.get("zone") == zone:
            return list(source.get("prefer") or [])
    return []


def _mana_symbols(view: dict[str, Any]) -> list[str] | None:
    """`view`'s own mana cost as one entry per mana ("1" for each generic,
    a colour letter for each pip) - `None` for a cost `stage_hypo_count`
    cannot pay for a probe (an unsupported hybrid/Phyrexian symbol, since
    `SYMBOL` only reads plain digits and plain colour letters)."""
    symbols: list[str] = []
    for symbol in SYMBOL.findall(view["mana_cost"]):
        if symbol.isdigit():
            symbols += ["1"] * int(symbol)
        elif symbol in COLOURED:
            symbols.append(symbol)
        else:
            return None
    return symbols


def _companion_candidates(pool: list[dict[str, Any]], board: Board) -> list[dict[str, Any]]:
    """Nonland cards from `pool` staging can plausibly pay for in a probe -
    mana value 1 to 3, a plain (non-hybrid, non-Phyrexian) cost, not already
    staged - one per distinct symbol multiset, cheapest first. Candidates
    `stage_hypo_count` tries in hand so a land it is testing has some other
    real hand card that could become newly payable, which is what
    `unlocks`/`unlocks_by_search`/`unlocks_by_basic_search` actually read
    (`hypothetical_facts.py`); a mana value above 1 needs `_mana_helpers` to
    add the rest of its cost too - the one land under test alone only pays
    for one mana."""
    staged = {view["name"] for _, view in board.cards}
    seen: set[tuple[str, ...]] = set()
    found = []
    for view in pool:
        if view["name"] in staged or "land" in card_types(view):
            continue
        if not (0 < view["mana_value"] <= 3) or "X" in view["mana_cost"]:
            continue
        symbols = _mana_symbols(view)
        if symbols is None:
            continue
        # A probe board is otherwise empty, so a spell needing a target the
        # board never has (a creature, an artifact, a spell on the stack)
        # never shows `castable_now` (`hypothetical_facts.py`'s item 3) no
        # matter the mana - excluded unless it targets a player, the one
        # target kind an otherwise-empty two-player board always has.
        oracle = view["oracle_text"].casefold()
        if "target" in oracle and "target player" not in oracle and "target opponent" not in oracle:
            continue
        key = tuple(sorted(symbols))
        if key in seen:
            continue
        seen.add(key)
        found.append(view)
    found.sort(key=lambda v: v["mana_value"])
    return found


def _mana_helpers(
    pool: list[dict[str, Any]], companion: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """Lands from `pool` covering every mana `companion` costs beyond the
    one land `stage_hypo_count` is already testing (which is assumed to pay
    the first mana, of any colour) - one per remaining coloured pip (a land
    producing it) and one per remaining generic (any land, colours reused
    where possible). `None` when `pool` cannot cover a pip at all."""
    symbols = (_mana_symbols(companion) or [])[1:]
    helpers: list[dict[str, Any]] = []
    fallback: dict[str, Any] | None = None
    for symbol in symbols:
        if symbol == "1":
            land = fallback or next((v for v in pool if "land" in card_types(v)), None)
        else:
            land = next((v for v in pool if "land" in card_types(v) and produces(v, symbol)), None)
            fallback = fallback or land
        if land is None:
            return None
        helpers.append(land)
    return helpers


def _real_hypo_facts(
    client: TestClient,
    board: Board,
    play: dict[str, Any],
    zone: str,
    candidate: dict[str, Any],
    companions: list[dict[str, Any]],
    library: list[dict[str, Any]] = (),
    extra_battlefield: list[dict[str, Any]] = (),
) -> dict[str, Any]:
    """The live engine's own facts (`enters`/`unlocks`/`unlocks_by_search`/
    `unlocks_by_basic_search`, `hypothetical_facts.build_hypothetical_facts`)
    for `candidate` staged into `zone`, on top of `board`'s own cards plus
    `companions` (extra hand cards, so `unlocks` has something real to
    unlock), `library` (extra library lands, so a `candidate` that searches -
    `unlocks_by_search`/`unlocks_by_basic_search` - has a real land in there
    its own search could find and that could itself unlock a companion) and
    `extra_battlefield` (extra untapped mana, so a companion costing more
    than `candidate` alone pays for still has something to pay it with) - a
    throwaway scratch game, deleted after. Staging cannot compute any of
    this offline (`selectors.HypotheticalRequired`); this is the one place
    it actually asks, exactly like the production route a real seat calls."""
    response = client.post(
        "/api/games", json={"players": [{"username": "alice"}, {"username": "bob"}]}
    )
    assert response.status_code == 200, response.text
    info = response.json()
    probe_id, admin_key = info["game_id"], info["admin_key"]
    try:
        when = play["when"]
        active = OPPONENT if when["whose_turn"] == "theirs" else ME
        force_turn_position(probe_id, Step(when["steps"][0]), active)
        probe_game = store.get(probe_id)
        assert probe_game is not None and probe_game.turn is not None
        probe_game.turn.turns_taken_by[ME] = board.turn
        for existing_zone, view in board.cards:
            add_card(client, probe_id, view["name"], ME, existing_zone)
        for view in companions:
            add_card(client, probe_id, view["name"], ME, "hand")
        for view in library:
            add_card(client, probe_id, view["name"], ME, "library")
        for view in extra_battlefield:
            add_card(client, probe_id, view["name"], ME, "battlefield")
        placed = add_card(client, probe_id, candidate["name"], ME, zone)
        uid = seat_uid(probe_id, ME)
        facts_response = client.get(
            f"/api/games/{probe_id}/playscripts/hypothetical-facts", params={"uid": uid}
        )
        assert facts_response.status_code == 200, facts_response.text
        return facts_response.json().get("cards", {}).get(placed["added"]["card_id"], {})
    finally:
        client.delete(f"/api/games/{probe_id}", params={"key": admin_key})


def stage_hypo_count(
    client: TestClient,
    board: Board,
    play: dict[str, Any],
    pool: list[dict[str, Any]],
    condition: dict[str, Any],
) -> None:
    """Satisfy a `true` count `condition` whose `select` reads a hypo-only
    field (`_HYPO_ONLY_FIELDS`) the offline pass could only guess at
    (`holds_loose`): try the deck's own real candidates for `condition`'s
    zone, best `move_prefer` tier first, each with no companion and then
    with every plausible one (`_companion_candidates`), and keep the first
    combination the live engine's own facts (`_real_hypo_facts`) actually
    confirm against `select` - never a guess staging trusts on its own."""
    zone, select = condition["zone"], condition["select"]
    present = board_present(board)
    prefer = move_prefer(play, zone)
    fits = [
        view
        for view in pool
        if loose_matches(select, view, present)
        and not breaks(board, play, zone, view)
        and not second_legend(board, zone, view)
    ]
    fits.sort(
        key=lambda view: next(
            (i for i, p in enumerate(prefer) if loose_matches(p, view, present)), len(prefer)
        )
    )
    needs_search = "unlocks_by_search" in select or "unlocks_by_basic_search" in select
    # Every OTHER false guard on the same zone that also reads a hypo-only
    # field: a combination that flips one of these true too (a fetch whose
    # library also carries a basic that would satisfy `unlocks_by_basic_
    # search`, when this play's own true condition only asked for
    # `unlocks_by_search`) belongs to a DIFFERENT, more specific play, and
    # accepting it here would stage a board that never applies for real.
    sibling_guards = [
        c
        for c in play["false"]
        if c["kind"] == "count" and c["zone"] == zone and _has_hypo(c.get("select"))
    ]
    for candidate in fits[:8]:
        # For a searching candidate (a fetch), real library lands its own
        # search clause could find - `unlocks_by_search`/`unlocks_by_basic_
        # search` read whether ONE of those would itself unlock a companion,
        # never the candidate's own mana (`hypothetical_facts.py`). Non-basic
        # finds come first: `unlocks_by_basic_search` needs a basic no more
        # specific play already covers.
        library_all = [
            view
            for view in pool
            if view["name"] != candidate["name"]
            and "land" in card_types(view)
            and finds(candidate, view)
        ]
        if "unlocks_by_basic_search" in select:
            # A basic must make the `[:4]` cut for the fact to have anything
            # to compute over at all.
            library_all.sort(key=lambda v: "basic" not in parse_type_line(v["type_line"])[0])
        elif "unlocks_by_search" in select:
            # Non-basic finds first: `unlocks_by_basic_search` should not
            # also flip true and trip a false guard the play deliberately
            # keeps separate (a basic-specific play covers that case).
            library_all.sort(key=lambda v: "basic" in parse_type_line(v["type_line"])[0])
        library = library_all[:4] if needs_search else []
        for companion in [None, *_companion_candidates(pool, board)[:8]]:
            companions = [companion] if companion else []
            if companion is not None and breaks(board, play, "hand", companion):
                continue
            extra_battlefield: list[dict[str, Any]] = []
            if companion is not None and companion["mana_value"] >= 2:
                helpers = _mana_helpers(pool, companion)
                if helpers is None or any(breaks(board, play, "battlefield", h) for h in helpers):
                    continue
                trial = board
                for helper in helpers:
                    trial = trial.plus("battlefield", helper)
                if any(c["kind"] == "untapped_mana" and holds(trial, c) for c in play["false"]):
                    continue
                extra_battlefield = helpers
            facts = _real_hypo_facts(
                client, board, play, zone, candidate, companions, library, extra_battlefield
            )
            hypo = {None: facts}
            if matches(select, candidate, present, hypo=hypo) and not any(
                matches(guard["select"], candidate, present, hypo=hypo) for guard in sibling_guards
            ):
                board.cards.append((zone, candidate))
                board.cards.extend(("hand", c) for c in companions)
                board.cards.extend(("library", c) for c in library)
                board.cards.extend(("battlefield", c) for c in extra_battlefield)
                return
    pytest.fail(f"no real candidate (land x companion) makes the live engine confirm {condition}")


def zone_floors(play: dict[str, Any]) -> dict[str, int]:
    floors = dict.fromkeys(ZONES, 0) | {"library": LIBRARY_FLOOR}
    for polarity in ("true", "false"):
        for c in play[polarity]:
            if c["kind"] != "zone_size" or c.get("side", "me") != "me":
                continue
            if polarity == "true" and c["op"] in (">=", ">"):
                floors[c["zone"]] = max(floors[c["zone"]], c["n"] + (c["op"] == ">"))
            if polarity == "false" and c["op"] in ("<=", "<"):
                floors[c["zone"]] = max(floors[c["zone"]], c["n"] + (c["op"] == "<="))
    return floors


def stage(client: TestClient, play: dict[str, Any], pool: list[dict[str, Any]]) -> Board:
    """A board, out of `pool`, on which every one of `play`'s true conditions
    holds, none of its false ones does, and every move has what it needs: its
    card, a card each search can find, mana for its cost, and a library past
    every size guard. `client` is only for a `true` count reading a hypo-only
    field (`stage_hypo_count`'s own real engine probe)."""
    board = Board()
    # The play's own `opposing_archetypes` gate (never a `true`/`false`
    # condition - `script_tools`'s own separate first check) needs a real
    # value too when it does not carry the wildcard: the default `"*"`
    # would never be in a play's own named list.
    archetypes = play.get("opposing_archetypes") or ["*"]
    if "*" not in archetypes:
        board.opposing_archetype = archetypes[0]
    present = board_present(board)
    picks = [
        item["pick"]
        for move in play["moves"]
        for item in move.get("answers") or []
        if item.get("decision") == "choose_cards" and item.get("pick")
    ]

    def searchers() -> list[dict[str, Any]]:
        found = []
        for move in play["moves"]:
            if move["kind"] in ("cast", "activate", "play_land"):
                zone, wanted = move_card(move, board)
                found += [view for view in board.zone(zone) if wanted(view)]
        return found

    def finds_every_pick(view: dict[str, Any]) -> int:
        targets = [
            v
            for v in board.zone("library")
            if any(loose_matches(p["select"], v, present) for p in picks)
        ]
        return 0 if all(finds(view, target) for target in targets) else 1

    counts = [c for c in play["true"] if c["kind"] == "count"]
    for condition in sorted(counts, key=lambda c: c["zone"] == "battlefield"):
        if condition.get("side", "me") == "opponent":
            if not holds_loose(board, condition):
                stage_opponent_count(board, condition)
            continue
        if _has_hypo(condition.get("select")):
            if not holds_loose(board, condition):
                stage_hypo_count(client, board, play, pool, condition)
            continue
        if condition["op"] not in GROWING and not holds_loose(board, condition):
            pytest.fail(f"staging only adds cards, and {condition} needs fewer")
        while not holds_loose(board, condition):
            add_chosen(
                board,
                play,
                pool,
                condition["zone"],
                lambda view, c=condition: loose_matches(c["select"], view, present),
                f"counted by {condition}",
                finds_every_pick,
            )

    for move in play["moves"]:
        if move["kind"] in ("cast", "activate", "play_land"):
            zone, wanted = move_card(move, board)
            if not any(wanted(view) for view in board.zone(zone)):
                add_chosen(board, play, pool, zone, wanted, f"the {move['kind']} move's card")

    for pick in picks:

        def findable(view: dict[str, Any], pick: dict[str, Any] = pick) -> bool:
            return loose_matches(pick["select"], view, present) and all(
                finds(s, view) for s in searchers()
            )

        if not any(findable(view) for view in board.zone("library")):
            prefer = list(pick.get("prefer") or [])

            def preferred(view: dict[str, Any], prefer: list[Any] = prefer) -> int:
                return next(
                    (i for i, p in enumerate(prefer) if loose_matches(p, view, present)),
                    len(prefer),
                )

            add_chosen(board, play, pool, "library", findable, "a card the search finds", preferred)

    cost = [symbol for move in play["moves"] for symbol in move_cost(move, board)]
    for colour, needed in Counter(s for s in cost if s in COLOURED).items():
        while sum(produces(view, colour) for view in mana_sources(board)) < needed:
            add_chosen(
                board,
                play,
                pool,
                "battlefield",
                lambda view, colour=colour: "land" in card_types(view) and produces(view, colour),
                f"a land making {{{colour}}}",
            )
    mana = [c["n"] + (c["op"] == ">") for c in play["true"] if c["kind"] == "untapped_mana"]
    while len(mana_sources(board)) < max([len(cost), *mana]):
        add_chosen(
            board,
            play,
            pool,
            "battlefield",
            lambda view: (
                "land" in card_types(view) and bool(mana_sources(Board([("battlefield", view)])))
            ),
            "an untapped land that makes mana",
        )

    for condition in play["true"]:
        if condition["kind"] == "life" and not holds(board, condition):
            op, n = condition["op"], condition["n"]
            board.life = n + (op == ">") if op in (">=", ">", "==") else n - (op == "<")
        if condition["kind"] == "turn" and not holds(board, condition):
            op, n = condition["op"], condition["n"]
            board.turn = n + (op == ">") if op in (">=", ">", "==") else max(0, n - (op == "<"))
        if condition["kind"] == "input" and not holds(board, condition):
            name, op, value = condition["name"], condition["op"], condition["value"]
            if op == "not_in":
                chosen = "an-archetype-not-in-that-list"
            elif op == "in":
                chosen = value[0] if isinstance(value, list) and value else value
            else:
                chosen = value
            if name == "opposing_archetype":
                board.opposing_archetype = chosen
            else:
                board.inputs[name] = chosen
    for condition in play["false"]:
        if condition["kind"] == "turn" and holds(board, condition):
            # Bump/drop `board.turn` to the nearest value past the guard: the
            # smallest change that makes `compare(board.turn, op, n)` False.
            op, n = condition["op"], condition["n"]
            board.turn = {
                "<=": n + 1,
                "<": n,
                ">=": max(0, n - 1),
                ">": n,
                "==": n + 1,
                "!=": n,
            }[op]

    selectors = play_selectors(play)
    for zone, floor in zone_floors(play).items():
        while len(board.zone(zone)) < floor:
            inert = [
                view
                for view in pool
                if not any(matches(selector, view, present, hypo={}) for selector in selectors)
            ]
            spells = [view for view in inert if "land" not in card_types(view)]
            chosen = (spells or inert or [None])[0]
            if chosen is None:
                pytest.fail(f"no card in the deck is filler the play never reads, for {zone}")
            board.cards.append((zone, chosen))

    unmet = [c for c in play["true"] if not holds_loose(board, c)]
    tripped = [c for c in play["false"] if holds(board, c)]
    assert not unmet and not tripped, (unmet, tripped, board.describe())
    return board


def stage_opponent_object_on_stack(client: TestClient, game_id: str) -> None:
    """Put a real object of the OPPONENT's own on top of an otherwise empty
    stack, priority handed back to me afterwards - what a `stack_top` true
    condition (`controller: "opponent"`) actually needs, which staging cards
    onto a board can never give by itself: gives the opponent a real,
    purely-generic-cost, untargeted instant from `universal_pool()` (found
    once, not one this deck's own corpus is assumed to carry - the opponent's
    real deck is never modeled) plus a land to pay it with, then drives the
    real pass/cast/pass sequence (CR 117) through the API so the object that
    ends up on the stack is a genuine `AbilityOnStack`/spell, not a stand-in.
    Called only for a play whose own true conditions actually read
    `stack_top`; every other play's stack stays empty exactly as before."""
    spell = next(
        view
        for view in universal_pool()
        if "instant" in card_types(view)
        and "faces" not in view
        and view["mana_cost"]
        and view["mana_value"] <= 1
        and (_mana_symbols(view) is not None)
        and all(symbol == "1" for symbol in _mana_symbols(view))
        and (
            "target" not in view["oracle_text"].casefold()
            or "target player" in view["oracle_text"].casefold()
            or "target opponent" in view["oracle_text"].casefold()
        )
    )
    add_card(client, game_id, spell["name"], OPPONENT, "hand")
    add_card(client, game_id, "Forest", OPPONENT, "battlefield")
    alice_uid = seat_uid(game_id, ME)
    bob_uid = seat_uid(game_id, OPPONENT)
    passed = client.post(
        f"/api/games/{game_id}/actions/pass-priority",
        params={"uid": alice_uid},
        json={"player": ME},
    )
    assert passed.status_code == 200, passed.text
    hand = state(client, game_id, bob_uid)["hands"][OPPONENT]
    [card] = named(hand, spell["name"])
    cast = client.post(
        f"/api/games/{game_id}/actions/cast",
        params={"uid": bob_uid},
        json={"player": OPPONENT, "card_id": card["id"]},
    )
    assert cast.status_code == 200, cast.text
    passed = client.post(
        f"/api/games/{game_id}/actions/pass-priority",
        params={"uid": bob_uid},
        json={"player": OPPONENT},
    )
    assert passed.status_code == 200, passed.text


def apply_board(client: TestClient, game_id: str, board: Board, play: dict[str, Any]) -> None:
    """Put the plan onto the real game at the play's first step, and hand me
    priority there."""
    when = play["when"]
    active = OPPONENT if when["whose_turn"] == "theirs" else ME
    force_turn_position(game_id, Step(when["steps"][0]), active)
    game = store.get(game_id)
    assert game is not None and game.turn is not None
    # A `turn` condition always reads MY own `own_turns_taken` (`schema.py`'s
    # `TurnCondition` has no `side`), regardless of whose turn `when` sets -
    # `force_turn_position` only moves step/active player, never this.
    game.turn.turns_taken_by[ME] = board.turn
    for zone, view in board.cards:
        add_card(client, game_id, view["name"], ME, zone)
    for zone, view in board.opponent_cards:
        add_card(client, game_id, view["name"], OPPONENT, zone)
    if board.life != 20:
        set_life(game_id, ME, board.life)
    if active == OPPONENT:
        hand_priority_to_me(client, game_id)


# -- every seed play ---------------------------------------------------------------------

#: Seed plays the engine cannot yet carry to completion, each with the
#: engine's own refusal. Empty means every play in the slice completes.
ENGINE_GAPS: dict[str, str] = {}


def with_gaps(cases: list[Any]) -> list[Any]:
    return [
        pytest.param(
            *case.values,
            id=case.id,
            marks=pytest.mark.xfail(reason=ENGINE_GAPS[case.id], strict=True),
        )
        if case.id in ENGINE_GAPS
        else case
        for case in cases
    ]


@pytest.mark.parametrize(("slug", "script_id"), with_gaps(seed_cases()))
def test_every_seed_play_completes_on_a_board_staged_from_its_decks_printings(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch, slug: str, script_id: str
) -> None:
    alice = seat_uid(game, ME)
    play = play_of(slug, script_id)
    board = stage(client, play, deck_pool(slug))
    register_corpus(client, game, alice, slug)
    apply_board(client, game, board, play)
    if any(c["kind"] == "stack_top" and c.get("controller") == "opponent" for c in play["true"]):
        stage_opponent_object_on_stack(client, game)
    server = mcp_server(client, monkeypatch, game)
    extra = {"opposing_archetype": board.opposing_archetype, "inputs": board.inputs or None}
    [judged] = [
        entry for entry in listing(server, alice, **extra)["scripts"] if entry["id"] == script_id
    ]
    assert judged["applies"] is True, (judged, board.describe())

    with opponent_passes(game):
        out = run(server, alice, script_id, **extra)["script_exit"]
        # `exit: null` with a `resume` token means the run stopped waiting on
        # someone else's own decision (never raced concurrently - see
        # `decline_opponent_yes_no`'s own docstring on why not) - answer it,
        # then resume the same run, until it actually finishes or truly
        # cannot make progress.
        for _ in range(5):
            if out.get("exit") is not None or not out.get("resume"):
                break
            if not decline_opponent_yes_no(client, game):
                break
            resume = out["resume"]
            resume = json.dumps(resume) if not isinstance(resume, str) else resume
            out = run(server, alice, script_id, resume=resume, **extra)["script_exit"]

    # `interrupted` ("someone else acted") is accepted too, but only once
    # every one of the script's own moves is done: `_foreign` (B7-09) counts
    # the OPPONENT legitimately answering their own required decision (this
    # play's own destroyed Tron piece handing its controller their own "you
    # may search for a basic land") as foreign no less than an unrelated
    # action would be, with no exception for "a decision this script's own
    # effect caused" - there is no way to answer that decision before the
    # run's own watermark is set, since it does not exist to answer until
    # the run itself creates it. A script cannot be faulted for what its own
    # resolution legitimately handed to another player.
    ok = out["exit"] == "chain_complete" or (
        out["exit"] == "interrupted" and out["moves_done"] == out["moves_total"]
    )
    assert ok, (out, board.describe())
    assert out["moves_done"] == out["moves_total"] == len(play["moves"]), out


# -- T18: the seat can reach and act in the opponent's turn window ------------------------
#
# A capability proof, not a behaviour change. If the seat's own judgement
# decides to hold a card and act on the opponent's turn, the machinery has to
# let it - and no play in the vendored corpus has ever cast anything on the
# opponent's turn (all fifteen `theirs` plays are `play_land` or `activate`),
# so the path was undemonstrated. These run the real runtime against the real
# engine; the opponent is a second client making real HTTP calls.


def _white_mana_for_me(client: TestClient, game_id: str, count: int = 2) -> None:
    for _ in range(count):
        add_card(client, game_id, "Plains", ME, "battlefield")


def test_the_seat_casts_a_permanent_from_hand_at_the_opponents_end_step(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clause (a): the opponent's end step, reached with priority, and a real
    cast from hand that resolves - a completed run, not `not_applicable`, not
    `unforeseen`, not refused."""
    alice = seat_uid(game, ME)
    play = flash_permanent_play("theirs-end-step-cast")
    register_corpus(client, game, alice, "boros-aggro", play)
    force_turn_position(game, Step("end_step"), OPPONENT)
    _white_mana_for_me(client, game)
    add_card(client, game, FLASH_PERMANENT, ME, "hand")
    hand_priority_to_me(client, game)
    server = mcp_server(client, monkeypatch, game)

    with opponent_passes(game):
        payload = run(server, alice, play["id"])

    assert payload["script_exit"]["exit"] == "chain_complete", payload["script_exit"]
    assert payload["script_exit"]["moves_done"] == 1
    after = state(client, game)
    assert named(after["battlefield"], FLASH_PERMANENT), (
        "the permanent never reached the battlefield on the opponent's turn"
    )
    assert not named(after["hands"][ME], FLASH_PERMANENT)


def test_the_seat_casts_in_response_to_the_opponents_spell_on_their_turn(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clause (b): `when.stack: "any"` with a `stack_top` condition naming the
    opponent as controller, firing while their spell is really on the stack.

    `conditions.stack_top` matches `stack[0]` only, so exactly one object is
    put on the stack before the run."""
    alice = seat_uid(game, ME)
    play = flash_permanent_play(
        "theirs-respond-to-stack",
        steps=("precombat_main",),
        stack="any",
        true=[
            {
                "kind": "stack_top",
                "controller": "opponent",
                "select": {"types_any": ["Instant"]},
            }
        ],
    )
    register_corpus(client, game, alice, "boros-aggro", play)
    force_turn_position(game, Step("precombat_main"), OPPONENT)
    _white_mana_for_me(client, game)
    add_card(client, game, FLASH_PERMANENT, ME, "hand")
    bob = seat_uid(game, OPPONENT)
    mountain = add_card(client, game, "Mountain", OPPONENT, "battlefield")
    bolt = add_card(client, game, "Lightning Bolt", OPPONENT, "hand")
    cast = client.post(
        f"/api/games/{game}/actions/cast",
        params={"uid": bob},
        json={
            "player": OPPONENT,
            "card_id": bolt["added"]["card_id"],
            "targets": [ME],
            "mana_sources": [mountain["added"]["card_id"]],
        },
    )
    assert cast.status_code == 200, cast.text
    server = mcp_server(client, monkeypatch, game)

    with opponent_passes(game):
        payload = run(server, alice, play["id"])

    assert payload["script_exit"]["exit"] == "chain_complete", payload["script_exit"]
    after = state(client, game)
    assert named(after["battlefield"], FLASH_PERMANENT), (
        "the permanent was never cast in response to the opponent's spell"
    )


def test_the_same_play_does_not_fire_on_the_seats_own_turn(
    client: TestClient, game: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clause (c): "acts on their turn" is a real property of the play, not an
    artefact of the harness.

    The identical shape, on the identical board, with only the active player
    changed, never casts. What it does instead is WAIT: `when.whose_turn:
    "theirs"` is a window the runner arms for rather than a condition it
    fails, so the run ends on its own time cap with no move made and the
    card still in hand. Either way the property under test holds - the seat
    did not act outside its window - and this is what the arming runner
    really does, not a shape invented for the assertion. `max_seconds` keeps
    the wait short; `wait: False` alone would not prove the run declines to
    act when it is given time."""
    alice = seat_uid(game, ME)
    play = flash_permanent_play("theirs-end-step-cast")
    register_corpus(client, game, alice, "boros-aggro", play)
    force_turn_position(game, Step("end_step"), ME)
    _white_mana_for_me(client, game)
    add_card(client, game, FLASH_PERMANENT, ME, "hand")
    server = mcp_server(client, monkeypatch, game)
    watermark = audit.last_seq(game)

    with opponent_passes(game):
        payload = run(server, alice, play["id"], max_seconds=2.0)

    exit_row = payload["script_exit"]
    assert exit_row["moves_done"] == 0, exit_row
    assert exit_row["exit"] != "chain_complete", exit_row
    after = state(client, game)
    assert not named(after["battlefield"], FLASH_PERMANENT)
    assert named(after["hands"][ME], FLASH_PERMANENT)
    assert not [
        row
        for row in audit.entries_since(game, watermark)
        if row["action"] == "cast" and row["detail"].get("player") == ME
    ], "the play cast something on the seat's own turn"


@pytest.mark.parametrize("step", ["declare_attackers", "declare_blockers"])
def test_a_combat_declaration_step_is_refused_by_the_schema(
    client: TestClient, game: str, step: str
) -> None:
    """Clause (d): the limit of this capability is discoverable from a test.
    A script never attacks or blocks, and the schema hard-refuses the two
    declaration steps rather than accepting a play that could never run."""
    from magic_cards_model_api.playscripts.schema import play_errors

    alice = seat_uid(game, ME)
    play = flash_permanent_play("theirs-combat", steps=(step,))
    assert "forbidden-kind" in {error.code for error in play_errors(play)}

    response = client.put(
        f"/api/games/{game}/seats/{alice}/scripts",
        params={"uid": alice},
        json={
            "deck_slug": "boros-aggro",
            "guides_revision": GUIDES_REVISION,
            "scripts": [play],
            "inputs": [],
        },
    )
    assert response.status_code == 422, response.text
