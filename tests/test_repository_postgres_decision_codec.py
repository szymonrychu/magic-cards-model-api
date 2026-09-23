"""`repository_postgres`'s `Decision`/`PendingDecision` JSON codec.

The postgres store writes a paused question to the `pending` column and reads
it back on every `/decide`; `commands.resume_command` then compares the
replayed question with the stored one by full frozen-dataclass equality. A
field the codec forgets is therefore not a cosmetic loss: it turns every
answer to that pause into a 409 "replay diverged" (game 07c5f818, 2026-09-14:
Cavern of Souls' `quality` came back as ""). These tests pin that every
engine `Decision` field survives, that rows written before a field existed
still decode, and that an unencodable field type fails loudly.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from magic_cards_model.machines.decisions import (
    Decision,
    DecisionKind,
    PendingAbility,
    PendingDecision,
)
from magic_cards_model.models.card_state import TargetKind, TargetRef

from magic_cards_model_api.repository_postgres import (
    _decision_from_json,
    _decision_to_json,
    _encode_dataclass,
    _pending_from_json,
    _pending_to_json,
)


def _through_json(payload: object) -> object:
    """What a JSONB column hands back: the payload serialised and re-parsed,
    so tuples really do come back as lists and UUIDs really are strings."""
    return json.loads(json.dumps(payload))


def _every_field_set() -> Decision:
    """A `Decision` with every field the engine defines set, and every field
    that has a default set to something other than that default.

    The value table is keyed by field name and checked against
    `dataclasses.fields(Decision)`, so the day the engine adds a field this
    test fails until the table - and therefore the round trip below - covers
    it too."""
    first, second, third = uuid4(), uuid4(), uuid4()
    bears = TargetRef(kind=TargetKind.CARD, card_id=first, zone_change_stamp=uuid4())
    bob = TargetRef(kind=TargetKind.PLAYER, username="bob")
    delver_one = PendingAbility(
        id=uuid4(),
        source_id=str(uuid4()),
        source_name="Delver of Secrets",
        text="At the beginning of your upkeep, look at the top card of your library.",
        controller="alice",
    )
    delver_two = PendingAbility(
        id=uuid4(),
        source_id=None,
        source_name="Delver of Secrets",
        text="At the beginning of your upkeep, look at the top card of your library.",
        controller="alice",
    )
    values: dict[str, object] = {
        "kind": DecisionKind.ASSIGN_COMBAT_DAMAGE,
        "player": "alice",
        "prompt": "assign combat damage",
        "source": "Lupine Harbingers",
        "source_id": str(uuid4()),
        "card": third,
        "amount": 4,
        "cards": (first, second),
        "players": ("alice", "bob"),
        "card_labels": ("first", "second"),
        "abilities": (delver_one, delver_two),
        "moved_label": "your hand",
        "kept_label": "bottom of your library",
        "affirmative_is_legal": False,
        "targets": (bears, bob),
        "target_maximum": 2,
        "target_minimum": 1,
        "options": ("white", "blue"),
        "quality": "color",
        "exact": False,
        "default_cards": (second,),
        "mana_sources": (third, first),
        "lethal_damage": (2, 0),
        "default_damage": ((bears, 3), (bob, 1)),
        "trample": True,
    }
    names = {f.name for f in dataclasses.fields(Decision)}
    assert set(values) == names, (
        f"Decision fields not covered by this test: {sorted(names - set(values))}; "
        f"stale entries: {sorted(set(values) - names)}"
    )
    for f in dataclasses.fields(Decision):
        if f.default is not dataclasses.MISSING:
            assert values[f.name] != f.default, f"{f.name} is still at its default"
    return Decision(**values)


def test_every_decision_field_round_trips_through_json() -> None:
    decision = _every_field_set()

    back = _decision_from_json(_through_json(_decision_to_json(decision)))

    differing = {
        f.name: (getattr(decision, f.name), getattr(back, f.name))
        for f in dataclasses.fields(Decision)
        if getattr(decision, f.name) != getattr(back, f.name)
    }
    assert back == decision, differing


def test_round_trip_restores_tuples_uuids_and_target_stamps_exactly() -> None:
    """Equality alone would not notice a lost `zone_change_stamp` - `TargetRef`
    deliberately leaves it out of `__eq__` - so it is checked by hand, along
    with the container and scalar types a list/str leak would change."""
    decision = _every_field_set()

    back = _decision_from_json(_through_json(_decision_to_json(decision)))

    assert type(back.cards) is tuple and all(type(c) is UUID for c in back.cards)
    assert type(back.mana_sources) is tuple and all(type(c) is UUID for c in back.mana_sources)
    assert type(back.default_damage) is tuple
    assert all(type(pair) is tuple for pair in back.default_damage)
    assert type(back.kind) is DecisionKind
    assert [t.zone_change_stamp for t in back.targets] == [
        t.zone_change_stamp for t in decision.targets
    ]
    assert [t.zone_change_stamp for t, _ in back.default_damage] == [
        t.zone_change_stamp for t, _ in decision.default_damage
    ]
    assert type(back.abilities) is tuple and len(back.abilities) == 2
    assert all(type(ability) is PendingAbility for ability in back.abilities)
    assert all(type(ability.id) is UUID for ability in back.abilities)
    assert back.abilities[0].source_id == decision.abilities[0].source_id
    assert back.abilities[1].source_id is None


def test_a_none_default_damage_stays_none() -> None:
    decision = Decision(kind=DecisionKind.MAY, player="you", prompt="you may draw a card")

    back = _decision_from_json(_through_json(_decision_to_json(decision)))

    assert back.default_damage is None
    assert back.default_cards is None
    assert back == decision


def test_the_pending_wrapper_round_trips_with_its_decision() -> None:
    pending = PendingDecision(seq=3, decision=_every_field_set(), rule="CR 510.1c")

    assert _pending_from_json(_through_json(_pending_to_json(pending))) == pending


def test_a_legacy_row_missing_the_newer_fields_decodes_with_their_defaults() -> None:
    """Exactly the shape the hand-written codec wrote before 2026-09-14 - no
    `quality`, `mana_sources`, `lethal_damage`, `default_damage` or `trample`
    key - as it already sits in production `games.pending` columns."""
    first, second = uuid4(), uuid4()
    legacy = {
        "kind": "choose_cards",
        "player": "alice",
        "prompt": "choose a card to discard",
        "source": "Faithless Looting",
        "source_id": None,
        "card": None,
        "amount": 1,
        "cards": [str(first), str(second)],
        "card_labels": [],
        "moved_label": "your graveyard",
        "kept_label": "your hand",
        "affirmative_is_legal": True,
        "targets": [{"kind": "player", "card_id": None, "username": "bob"}],
        "target_maximum": None,
        "target_minimum": 0,
        "options": [],
        "exact": True,
        "default_cards": [str(second)],
    }

    decoded = _decision_from_json(legacy)

    assert decoded == Decision(
        kind=DecisionKind.CHOOSE_CARDS,
        player="alice",
        prompt="choose a card to discard",
        source="Faithless Looting",
        amount=1,
        cards=(first, second),
        moved_label="your graveyard",
        kept_label="your hand",
        targets=(TargetRef(kind=TargetKind.PLAYER, username="bob"),),
        default_cards=(second,),
    )
    assert decoded.quality == ""
    assert decoded.mana_sources == ()
    assert decoded.lethal_damage == ()
    assert decoded.default_damage is None
    assert decoded.trample is False


def test_a_row_missing_a_required_field_is_refused_by_name() -> None:
    with pytest.raises(KeyError, match="prompt"):
        _decision_from_json({"kind": "may", "player": "you"})


@dataclass(frozen=True)
class _CarriesAnUnencodableType:
    name: str
    tags: frozenset[str] = frozenset()


def test_an_unencodable_field_type_fails_loudly_naming_the_field() -> None:
    """Never a silent drop: a field whose type the codec has no rule for is a
    refusal at write time, and the error says which field. The type is what
    is refused, so even the field's empty default does not slip through."""
    with pytest.raises(TypeError, match="tags"):
        _encode_dataclass(_CarriesAnUnencodableType(name="x", tags=frozenset({"a"})))
    with pytest.raises(TypeError, match="tags"):
        _encode_dataclass(_CarriesAnUnencodableType(name="x"))
