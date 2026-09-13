"""The play-script schema: design D2, closed and bounded.

`decks/<slug>/plays/<id>.json` is a declarative chain a seat runs through the
seatkit runner. `Play` is its model: every object forbids unknown keys, every
union is discriminated on `kind`, every list is bounded, and no field or kind
can iterate, attack, block, mulligan, concede or pass. The vocabulary is
closed at seven condition kinds and four move kinds.

`play_errors` is the same judgement as coded errors - one `PlaysError` per
refused shape - using the codes shared verbatim with mtg-decks'
`scripts/check_plays.py`. Pydantic stops judging a list's items once the list
is too long, and skips a model's own check once a field of it failed; the two
supplements below keep reporting what the stdlib gate reports in those cases.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from ..seatkit import AUTOPASS_STEPS

SCHEMA_VERSION = 1
WILDCARD = "*"
MAX_MOVES = 4
MAX_ANSWERS = 3
MAX_NEXT = 3
MAX_CONDITIONS = 8
MAX_STEPS = 4
MAX_PRIORITY = 100

COMBAT_DECLARATION_STEPS = ("declare_attackers", "declare_blockers")
#: The steps a script may name: the autopass steps, minus the two combat
#: declarations, because scripts never attack or block.
STEPS = tuple(step for step in AUTOPASS_STEPS if step not in COMBAT_DECLARATION_STEPS)
CONDITION_KINDS = (
    "count",
    "zone_size",
    "life",
    "stack_top",
    "land_drop_available",
    "untapped_mana",
    "input",
)
MOVE_KINDS = ("activate", "cast", "play_land", "answer")
COUNT_ZONES = ("battlefield", "graveyard", "exile", "hand", "stack", "library", "sideboard")
SIZE_ZONES = ("hand", "library", "graveyard", "exile", "sideboard")
HIDDEN_ZONES = ("hand", "library", "sideboard")
SIDES = ("me", "opponent", "any")
OPS = (">=", "<=", "==", "!=", ">", "<")
INPUT_OPS = ("eq", "ne", "in", "not_in", "<", "<=", ">", ">=")

CODES = (
    "malformed-play",
    "unknown-field",
    "id-mismatch",
    "bounds-exceeded",
    "forbidden-kind",
    "hidden-zone-side-any",
    "card-name-in-field",
    "phrase-too-narrow",
    "unknown-next",
    "next-cycle",
    "chain-too-deep",
    "too-many-plays",
    "too-many-active-for-archetype",
    "unknown-archetype-key",
    "unknown-input",
    "invalid-input-value",
    "malformed-inputs",
)


@dataclass(frozen=True)
class PlaysError:
    """One refused shape: a code from `CODES`, the file it is in, and why."""

    code: str
    path: str
    message: str

    def __post_init__(self) -> None:
        if self.code not in CODES:
            raise ValueError(f"unknown play error code {self.code!r}")

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


NonBlank = Annotated[str, Field(pattern=r"\S")]
ArchetypeKey = Annotated[str, Field(pattern=r"^(\*|[a-z0-9]+(-[a-z0-9]+)*)$")]
AtLeastZero = Annotated[int, Field(ge=0)]
Op = Literal[OPS]
Side = Literal[SIDES]
StepName = Literal[STEPS]


class _Strict(BaseModel):
    """Unknown keys refused, no coercion. An optional field is either left
    out or given a value: its type does not admit `null`, so an explicit
    `null` is refused rather than read as absent."""

    model_config = ConfigDict(extra="forbid", strict=True)


# -- selectors ---------------------------------------------------------------


class Comparison(_Strict):
    """`{op, n}` against a number on the card."""

    op: Op
    n: AtLeastZero


class CounterComparison(_Strict):
    """`{name, op, n}`: how many counters of one kind (not a card name)."""

    name: NonBlank
    op: Op
    n: AtLeastZero


class Selector(_Strict):
    """Characteristics of one card, all optional, combined with AND. No field
    takes a card name: type words come from the type line, text fields are
    template phrases matched case-insensitively."""

    types_all: list[NonBlank] = None
    types_any: list[NonBlank] = None
    subtypes_any: list[NonBlank] = None
    supertypes_any: list[NonBlank] = None
    colors_any: list[NonBlank] = None
    mana_value: Comparison = None
    power: Comparison = None
    toughness: Comparison = None
    keywords_any: list[NonBlank] = None
    tapped: bool = None
    counters: CounterComparison = None
    ability_text_has: NonBlank = None
    oracle_has: NonBlank = None


# -- conditions --------------------------------------------------------------


class CountCondition(_Strict):
    """How many cards matching `select` are in a zone, by side. A hidden zone
    (hand, library, sideboard) must name `me` or `opponent`."""

    kind: Literal["count"]
    zone: Literal[COUNT_ZONES]
    side: Side
    op: Op
    n: AtLeastZero
    select: Selector
    known_only: bool = None

    @model_validator(mode="after")
    def _hidden_zone_names_a_side(self) -> CountCondition:
        if self.zone in HIDDEN_ZONES and self.side == "any":
            raise PydanticCustomError(
                "hidden_zone_side_any",
                "a count over the {zone} must name side me or opponent, not any",
                {"zone": self.zone},
            )
        return self


class ZoneSizeCondition(_Strict):
    """A zone's public card count, by side."""

    kind: Literal["zone_size"]
    zone: Literal[SIZE_ZONES]
    side: Side
    op: Op
    n: AtLeastZero


class LifeCondition(_Strict):
    kind: Literal["life"]
    side: Side
    op: Op
    n: AtLeastZero


class StackTopCondition(_Strict):
    """The top object of the stack: whose, and matching `select`."""

    kind: Literal["stack_top"]
    controller: Literal["me", "opponent"]
    select: Selector


class LandDropCondition(_Strict):
    kind: Literal["land_drop_available"]
    value: bool


class UntappedManaCondition(_Strict):
    kind: Literal["untapped_mana"]
    op: Op
    n: AtLeastZero


class InputCondition(_Strict):
    """A fact only the agent knows: a declared input or `opposing_archetype`."""

    kind: Literal["input"]
    name: NonBlank
    op: Literal[INPUT_OPS]
    value: Any


Condition = Annotated[
    CountCondition
    | ZoneSizeCondition
    | LifeCondition
    | StackTopCondition
    | LandDropCondition
    | UntappedManaCondition
    | InputCondition,
    Field(discriminator="kind"),
]


# -- moves -------------------------------------------------------------------


class Pick(_Strict):
    select: Selector
    count: Annotated[int, Field(ge=1)] = None
    prefer: list[Selector] = None


class Answer(_Strict):
    """How to answer one pending decision of kind `decision`."""

    decision: NonBlank
    pick: Pick = None
    accept: bool = None
    to_graveyard: Selector = None
    to_bottom: Selector = None


class ZonedCard(_Strict):
    zone: Literal[COUNT_ZONES]
    select: Selector


class CastTarget(_Strict):
    select: Selector
    side: Side


class LandCard(_Strict):
    select: Selector


class ActivateMove(_Strict):
    kind: Literal["activate"]
    source: ZonedCard
    ability_text_has: NonBlank
    sacrifice: Literal["source", "none"] = None
    answers: list[Answer] = Field(default_factory=list, max_length=MAX_ANSWERS)


class CastMove(_Strict):
    kind: Literal["cast"]
    card: ZonedCard
    targets: list[CastTarget] = Field(default_factory=list)
    mana: Literal["auto"] = None
    answers: list[Answer] = Field(default_factory=list, max_length=MAX_ANSWERS)


class PlayLandMove(_Strict):
    kind: Literal["play_land"]
    card: LandCard
    answers: list[Answer] = Field(default_factory=list, max_length=MAX_ANSWERS)


class AnswerMove(_Strict):
    """Answers for a decision already pending."""

    kind: Literal["answer"]
    answers: list[Answer] = Field(min_length=1, max_length=MAX_ANSWERS)


Move = Annotated[
    ActivateMove | CastMove | PlayLandMove | AnswerMove,
    Field(discriminator="kind"),
]


# -- the play ----------------------------------------------------------------


class When(_Strict):
    whose_turn: Literal["mine", "theirs", "either"]
    steps: list[StepName] = Field(min_length=1, max_length=MAX_STEPS)
    stack: Literal["empty", "any"]


class Play(_Strict):
    """One play script. `name` and `intent` are prose nothing executes;
    `priority` 0 disables it; `true` must all hold and `false` must all not
    hold; `next` names plays of the same deck tried after the moves."""

    schema_version: Literal[1]
    id: NonBlank
    name: NonBlank
    intent: NonBlank = None
    priority: Annotated[int, Field(ge=0, le=MAX_PRIORITY)]
    opposing_archetypes: list[ArchetypeKey] = Field(min_length=1)
    when: When
    true: list[Condition] = Field(default_factory=list, max_length=MAX_CONDITIONS)
    false: list[Condition] = Field(default_factory=list, max_length=MAX_CONDITIONS)
    moves: list[Move] = Field(min_length=1, max_length=MAX_MOVES)
    next: list[NonBlank] = Field(default_factory=list, max_length=MAX_NEXT)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version_is_not_a_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise PydanticCustomError("literal_error", "Input should be 1")
        return value


def json_schema() -> dict[str, Any]:
    """`Play` as JSON Schema, for `contract/playscript.schema.json`."""
    schema = Play.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema


# -- coded errors ------------------------------------------------------------

_ITEM_ADAPTERS: dict[str, TypeAdapter[Any]] = {
    "moves": TypeAdapter(Move),
    "true": TypeAdapter(Condition),
    "false": TypeAdapter(Condition),
    "answers": TypeAdapter(Answer),
    "next": TypeAdapter(NonBlank),
    "steps": TypeAdapter(StepName),
}
_BOUND_TYPES = ("too_long", "greater_than_equal", "less_than_equal", "greater_than", "less_than")
_MISSING = object()


def _walk(doc: Any, loc: tuple[Any, ...]) -> tuple[str, Any]:
    """The dotted location of `loc` in `doc`, and the value found there.

    Pydantic puts a discriminated union's tag into the location after the
    list index; it names no key of the document, so it is skipped."""
    text, node = "", doc
    for part in loc:
        if (
            isinstance(node, dict)
            and isinstance(part, str)
            and part not in node
            and node.get("kind") == part
        ):
            continue
        if isinstance(part, int):
            text += f"[{part}]"
            node = node[part] if isinstance(node, list) and 0 <= part < len(node) else _MISSING
        else:
            text = f"{text}.{part}" if text else str(part)
            node = node[part] if isinstance(node, dict) and part in node else _MISSING
    return text, node


def _last_field(loc: tuple[Any, ...]) -> str | None:
    return next((part for part in reversed(loc) if isinstance(part, str)), None)


def _code(error: dict[str, Any], loc: tuple[Any, ...]) -> str:
    kind = error["type"]
    if kind == "extra_forbidden":
        return "unknown-field"
    if kind == "hidden_zone_side_any":
        return "hidden-zone-side-any"
    if kind in _BOUND_TYPES:
        return "bounds-exceeded"
    if kind == "too_short":
        return "malformed-play" if _last_field(loc) == "opposing_archetypes" else "bounds-exceeded"
    if kind == "union_tag_invalid":
        value = error.get("input")
        tag = value.get("kind") if isinstance(value, dict) else None
        return "forbidden-kind" if isinstance(tag, str) else "malformed-play"
    if (
        kind == "literal_error"
        and len(loc) >= 2
        and loc[-2] == "steps"
        and error.get("input") in COMBAT_DECLARATION_STEPS
    ):
        return "forbidden-kind"
    return "malformed-play"


def _message(error: dict[str, Any], code: str) -> str:
    if code == "forbidden-kind" and error["type"] == "literal_error":
        return f"{error['input']!r} is a combat declaration step; scripts do not attack or block"
    if code == "forbidden-kind":
        return f"kind {error['input'].get('kind')!r} is not in the closed vocabulary"
    if code == "unknown-field":
        return "is not a field of this schema"
    return error["msg"]


def _translate(
    errors: list[dict[str, Any]], doc: Any, prefix: tuple[Any, ...]
) -> Iterator[tuple[str, str, str]]:
    for error in errors:
        loc = (*prefix, *error["loc"])
        where, found = _walk(doc, loc)
        code = _code(error, loc)
        yield code, where, _message(error, code)
        if error["type"] != "too_long":
            continue
        adapter = _ITEM_ADAPTERS.get(_last_field(loc) or "")
        if adapter is None or not isinstance(found, list):
            continue
        for index, item in enumerate(found):
            try:
                adapter.validate_python(item)
            except ValidationError as exc:
                yield from _translate(exc.errors(), doc, (*loc, index))


def _hidden_zone_side_any(doc: Any) -> Iterator[tuple[str, str, str]]:
    """Every count over a hidden zone naming side `any`, whatever else is
    wrong with it - pydantic runs the model's own check only once every field
    of the condition validated."""
    if not isinstance(doc, dict):
        return
    for polarity in ("true", "false"):
        conditions = doc.get(polarity)
        if not isinstance(conditions, list):
            continue
        for index, condition in enumerate(conditions):
            if (
                isinstance(condition, dict)
                and condition.get("kind") == "count"
                and condition.get("zone") in HIDDEN_ZONES
                and condition.get("side") == "any"
            ):
                yield (
                    "hidden-zone-side-any",
                    f"{polarity}[{index}]",
                    f"a count over the {condition['zone']} must name side me or opponent, not any",
                )


def play_errors(doc: Any, path: str = "play") -> list[PlaysError]:
    """Every structural refusal of one play document, as coded errors."""
    found: list[tuple[str, str, str]] = []
    try:
        Play.model_validate(doc)
    except ValidationError as exc:
        found.extend(_translate(exc.errors(), doc, ()))
    found.extend(_hidden_zone_side_any(doc))
    reported: set[tuple[str, str]] = set()
    errors = []
    for code, where, message in found:
        if (code, where) in reported:
            continue
        reported.add((code, where))
        errors.append(PlaysError(code, path, f"{where}: {message}" if where else message))
    return errors
