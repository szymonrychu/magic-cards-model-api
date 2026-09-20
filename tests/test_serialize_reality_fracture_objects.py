"""The Reality Fracture bump's (engine 03a891c) new zones and objects,
serialized - proving each reaches the wire generically, with no per-object
wiring this api had to add:

- CR 114.2 emblems, in the shared command zone (`Game.create_emblem`).
- The `empower_jace`/Heartwood predefined tokens (`models/token.py`'s
  `JACE`/`HEARTWOOD`), a planeswalker and an artifact respectively, put
  through `create_tokens` the same way any other predefined token is.

Each object is created white-box, the same way `test_serialize_attachments
.py`'s exile-link test sets `ExileAnnotation` directly: the point under test
is the serializer's generic `views()`/`_card_view` path, not the effect that
creates the object.
"""

from __future__ import annotations

from conftest import find
from magic_cards_model.models.token import HEARTWOOD, JACE, create_tokens

from magic_cards_model_api import serialize
from magic_cards_model_api.store import store


def test_an_emblem_reaches_the_command_zone_on_the_wire(client, game):
    engine = store.get(game)
    engine.create_emblem("alice", "Creatures you control get +1/+1.", "Test Planeswalker")

    view = serialize.state_view(engine, game)
    emblem = find(view["command"], "Test Planeswalker emblem")
    assert emblem["zone"] == "command"
    assert emblem["controller"] == "alice"
    assert emblem["type_line"] == "Emblem"
    assert "Creatures you control get +1/+1." in (emblem["oracle_text"] or "")


def test_a_jace_token_reaches_the_battlefield_as_a_planeswalker(client, game):
    engine = store.get(game)
    create_tokens(engine, JACE, "alice")

    view = serialize.state_view(engine, game)
    jace = find(view["battlefield"], "Jace")
    assert "Planeswalker" in jace["type_line"]
    # Jace's own printed loyalty is 0 (CR 701.71a's own quoted wording), so
    # `_enter_with_loyalty` adds none - a real, zero-count `Counters` entry is
    # never stored, matching every other 0-count counter kind on the wire.
    assert jace["counters"].get("loyalty", 0) == 0
    ability_texts = " ".join(a.get("text", "") for a in jace["abilities"])
    assert "Surveil 1" in ability_texts
    assert "Draw a card" in ability_texts


def test_a_heartwood_token_reaches_the_battlefield_as_an_artifact(client, game):
    engine = store.get(game)
    create_tokens(engine, HEARTWOOD, "alice")

    view = serialize.state_view(engine, game)
    # CR 111.4: no `name` on `TokenDefinition.HEARTWOOD`, so the fallback
    # naming applies - "Heartwood Token", not the bare "Heartwood" CR
    # 111.10x's own reminder text uses.
    heartwood = find(view["battlefield"], "Heartwood Token")
    assert "Artifact" in heartwood["type_line"]
    assert set(heartwood["colors"]) == {"R", "G"}
