"""FD5 review finding (ai_seat_brief.md:458): the brief's worked DSL-first
example - a Devoted Druid + Vizier of Remedies mana loop, written with
selectors - has to select against the real printed cards, or a seat that
copies it verbatim runs a loop that cannot activate anything.

This builds a real Devoted Druid on a real battlefield through the ordinary
API (`conftest.add_card`, a real Scryfall printing - no hand-written card
data) and checks the example's own `oracle_has`/`ability_text_has` strings
against what that card actually carries, exactly the way `playscripts.
selectors` matches them (case-insensitive substring - see `selectors.py`'s
`_match_card`).
"""

from __future__ import annotations

import json
import re

from conftest import add_card, find, state
from fastapi.testclient import TestClient

from magic_cards_model_api import ai_seat


def _worked_example() -> dict:
    """The JSON object under "A worked example: a Devoted Druid + Vizier of
    Remedies mana loop" in the seat brief, with the brief's own `{{`/`}}`
    escaping undone by going through `build_prompt` exactly as a real seat's
    dispatch does, rather than re-implementing that unescaping here."""
    filled = ai_seat.build_prompt(
        uid="uid-1", game_id="game-1", player="alice", api_base="http://testserver"
    )
    marker = "A worked example: a Devoted Druid + Vizier of Remedies mana loop"
    start = filled.index(marker)
    body = filled[start:]
    # The example is the first top-level `{...}` block after the marker,
    # indented as a markdown code block - find its matching closing brace by
    # depth rather than by a fixed line count, so this survives reformatting.
    open_at = body.index("{")
    depth = 0
    for i, ch in enumerate(body[open_at:], start=open_at):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(body[open_at : i + 1])
    raise AssertionError("no balanced JSON object found after the worked-example marker")


def test_the_worked_example_is_valid_json() -> None:
    example = _worked_example()
    assert example["kind"] == "repeat"
    assert len(example["moves"]) == 2


def test_the_worked_examples_selectors_match_the_real_devoted_druid(client: TestClient, game: str):
    """Each move's `source.select` must match the real Devoted Druid, and
    each move's own `ability_text_has` must match one of its two real
    activated abilities - not text that appears nowhere on the card."""
    add_card(client, game, "Devoted Druid", "alice", "battlefield")
    board = state(client, game)
    druid = find(board["battlefield"], "Devoted Druid")

    example = _worked_example()
    oracle = (druid.get("oracle_text") or "").casefold()
    ability_texts = [(a.get("text") or "").casefold() for a in druid["abilities"]]

    for move in example["moves"]:
        select = move["source"]["select"]
        oracle_has = select["oracle_has"].casefold()
        assert oracle_has in oracle, (
            f"the brief's select {select['oracle_has']!r} is not on Devoted Druid's real "
            f"oracle text ({druid.get('oracle_text')!r}) - the selector matches no card"
        )
        wanted = move["ability_text_has"].casefold()
        real_texts = [a.get("text") for a in druid["abilities"]]
        assert any(wanted in text for text in ability_texts), (
            f"the brief's ability_text_has {move['ability_text_has']!r} matches none of "
            f"Devoted Druid's real activated abilities ({real_texts!r}) - a seat copying "
            "this loop verbatim has no ability to activate"
        )

    # And the two moves must pick two *different* abilities - a loop that
    # names the same ability twice is not the tap-then-untap loop it claims
    # to be.
    picked = {move["ability_text_has"].casefold() for move in example["moves"]}
    assert len(picked) == 2, "the worked example's two moves must activate two different abilities"


def test_worked_example_selector_strings_never_appear_verbatim_elsewhere_as_placeholders() -> None:
    """Guards against re-introducing the reviewed defect verbatim (the old
    'Remove a -1/-1 counter' / 'Add {G}{G}' text was never printed on any
    card carrying Devoted Druid's mechanic)."""
    filled = ai_seat.load_brief()
    assert not re.search(r"Remove a -1/-1 counter\b", filled), (
        "the brief still claims Devoted Druid's mana or untap ability reads "
        "'Remove a -1/-1 counter' - the real card puts one on, it does not remove one"
    )
