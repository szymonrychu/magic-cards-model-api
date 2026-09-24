"""The play-script schema: design D2, closed and bounded.

`decks/<slug>/plays/<id>.json` is a declarative chain a seat runs through the
seatkit runner. `Play` is its model: every object forbids unknown keys, every
union is discriminated on `kind`, every list is bounded (`maxItems` in the
exported JSON Schema, an explicit bound on every list field below), and no
field or kind can iterate, attack, block, mulligan, concede or pass. The
vocabulary is closed at eight condition kinds and four move kinds; a `cast`
names its cost from `CAST_COSTS` (the engine's own cast option kinds), and an
answer names its decision from `DECISION_KINDS` (the api's pending decision
kinds, less the combat declarations and the mulligan a script never makes).
`when.steps` names each step once, and `opposing_archetypes` is either `["*"]`
or named keys, never both. An answer's fields fit its decision kind: a
pay-amount decision (`AMOUNT_DECISION_KINDS`) is answered with `amount` alone,
and no other kind takes one (`answer-shape-mismatch`). A `cast` or `activate`
target is a card by selector and side, or a player; an `activate` sacrifices
its source, nothing, or permanents chosen by selector, and independently
discards nothing or hand cards chosen by selector (never the source itself -
that is `sacrifice`'s own "source" value; CR 701.9's discard always reads
from hand, never the battlefield). A selector's optional
`missing_from` names zones (`MISSING_FROM_ZONES`, at least one), a side
(`MISSING_FROM_SIDES`) and what it compares (`MISSING_FROM_BY`); a word outside
those is `malformed-play` and no zones `bounds-exceeded`, so it adds no code.

`play_errors` is the same judgement as coded errors - one `PlaysError` per
refused shape - using the codes shared verbatim with mtg-decks'
`scripts/check_plays.py`. Pydantic stops judging a list's items once the list
is too long, and skips a model's own check once a field of it failed; the
supplements below keep reporting what the stdlib gate reports in those cases.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from ..decisions import ANSWER_FIELD_FOR_KIND, ANSWERABLE_KINDS, TOOL_FIELD_FOR_KIND
from ..seatkit import AUTOPASS_STEPS
from .selectors import BASIC_LAND_TYPES

SCHEMA_VERSION = 1
WILDCARD = "*"
MAX_MOVES = 4
MAX_ANSWERS = 3
MAX_NEXT = 3
MAX_CONDITIONS = 8
MAX_STEPS = 4
MAX_PRIORITY = 100
#: A selector word list (types, subtypes, supertypes, colors, keywords) is a
#: set of alternatives drawn from one characteristic. The largest closed
#: vocabulary any of them draws from is CR 205.2a's fifteen card types; a
#: longer list is no longer describing a characteristic.
MAX_SELECTOR_WORDS = 15
#: One entry per target the cast announces, in order. Bounded like the rest
#: of a play's fan-out (moves, answers, next): a spell asking for more targets
#: than this is left to the seat rather than scripted.
MAX_TARGETS = 6
#: Ordered tie-breakers after `pick.select`, the same fan-out as `moves`.
MAX_PREFER = 4
#: A `choose_players` answer's own `players` list: "me" and "opponent" are
#: the only tokens the vocabulary has, so two is every legal length already.
MAX_PLAYERS_ANSWER = 2
#: `ties: "any"` takes the first `count` still-tied candidates by sorted
#: name instead of exiting `ambiguous`; left out, behaviour is unchanged.
TIES = ("any",)
#: Named archetype keys of one play, or of one `in`/`not_in` input list. The
#: widest enum a play names from is `opposing_archetype`, `*` plus the matchup
#: stems of a deck's newest analysis run (19 in the largest run at the time of
#: writing); a play meant for every matchup says `*`.
MAX_ARCHETYPES = 32
MAX_INPUT_VALUES = 32
#: `note`'s own bound: short enough for an agent to read mid-game, long
#: enough for one sentence of reasoning.
MAX_NOTE_LENGTH = 240

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
    "turn",
    "input",
)
MOVE_KINDS = ("activate", "cast", "play_land", "answer")
COUNT_ZONES = ("battlefield", "graveyard", "exile", "hand", "stack", "library", "sideboard")
SIZE_ZONES = ("hand", "library", "graveyard", "exile", "sideboard")
HIDDEN_ZONES = ("hand", "library", "sideboard")
SIDES = ("me", "opponent", "any")
#: `Selector.missing_from`: the zones it reads are a count's own, on one named
#: side (never `any`), and it compares either subtypes or card names.
MISSING_FROM_ZONES = COUNT_ZONES
MISSING_FROM_SIDES = ("me", "opponent")
MISSING_FROM_BY = ("subtype", "name")
OPS = (">=", "<=", "==", "!=", ">", "<")
INPUT_OPS = ("eq", "ne", "in", "not_in", "<", "<=", ">", ">=")
#: The cost a `cast` move is announced under. `normal` is the printed mana
#: cost. The rest are the engine's cast option kinds, as a card view's
#: `cast_options[].keyword` and `CastRequest.keyword` name them, lowercased:
#: every keyword `machines/casting.py::_option_from_keyword` turns into a cast
#: option, the `Adventure` and `Airbend` options `keyword_cast_options` and
#: `marker_cast_options` hand out, and `printed` - the card's own CR 118.9
#: alternative cost (`AlternativeCostSource.PRINTED`, a Force of Negation
#: pitch), which `CastRequest.alternative_cost` announces. `impending`
#: (CR 702.176a) is named although the engine offers no impending cast yet: a
#: runner refuses a cost no live cast option offers, the same as any other.
#: `tests/test_playscript_schema.py` reads the engine's own source, so a cast
#: option the engine learns fails that test until it is named here.
CAST_COSTS = (
    "normal",
    "adventure",
    "airbend",
    "bestow",
    "dash",
    "disguise",
    "escape",
    "evoke",
    "flashback",
    "foretell",
    "harmonize",
    "impending",
    "madness",
    "mayhem",
    "megamorph",
    "morph",
    "plot",
    "printed",
    "prowl",
    "sneak",
    "spectacle",
    "warp",
)
#: The decision kinds an answer may name: every `DecisionKind` the api can
#: pend (`decisions.ANSWERABLE_KINDS`), by value.
DECISION_KINDS = tuple(sorted(kind.value for kind in ANSWERABLE_KINDS))
#: Pending decision kinds a script never answers: scripts do not attack, block,
#: assign combat damage or mulligan, the same reason the combat declaration
#: steps are forbidden.
FORBIDDEN_DECISION_KINDS = (
    "assign_combat_damage",
    "declare_attackers",
    "declare_blockers",
    "mulligan",
)
#: The decision kinds answered with a number rather than yes/no or cards: the
#: kinds `decisions.ANSWER_FIELD_FOR_KIND` answers on the engine's `amount`
#: field (`pay_energy_amount`, `pay_life_amount` - an integer from 0 up to the
#: decision's own `amount`, the payer's energy or life). Their answer item
#: carries `amount` and nothing else.
AMOUNT_DECISION_KINDS = tuple(
    sorted(kind.value for kind, field in ANSWER_FIELD_FOR_KIND.items() if field == "amount")
)
#: `amount: "max"` pays the most the decision allows (its own `amount`).
AMOUNT_POLICIES = ("max",)
#: The answer fields an amount decision never takes.
NON_AMOUNT_ANSWER_FIELDS = (
    "pick",
    "accept",
    "to_graveyard",
    "to_bottom",
    "choice",
    "pile_one",
    "players",
)
#: The decision kinds answered by naming a string from an offered (or, for
#: `choose_a_quality`, sometimes open) list: the kinds the api's
#: `decisions.TOOL_FIELD_FOR_KIND` answers on `choice` (Cavern of Souls' and
#: Utopia Sprawl's "as this enters, choose a ..." CR 614.12a, and CR 700.2a's
#: "choose ... mode" abilities). O6.
CHOICE_DECISION_KINDS = tuple(
    sorted(kind.value for kind, field in TOOL_FIELD_FOR_KIND.items() if field == "choice")
)

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
    "too-many-advice",
    "advice-only-chained",
    "unknown-archetype-key",
    "unknown-input",
    "invalid-input-value",
    "malformed-inputs",
    "unknown-decision-kind",
    "unknown-cast-cost",
    "duplicate-step",
    "wildcard-with-keys",
    "answer-shape-mismatch",
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
#: `note`'s own type: a non-blank string, bounded, unlike `NonBlank`'s other
#: prose fields (`name`, `intent`), which carry no length bound of their own.
Note = Annotated[str, Field(pattern=r"\S", max_length=MAX_NOTE_LENGTH)]
ArchetypeKey = Annotated[str, Field(pattern=r"^(\*|[a-z0-9]+(-[a-z0-9]+)*)$")]
AtLeastZero = Annotated[int, Field(ge=0)]
Op = Literal[OPS]
Side = Literal[SIDES]
StepName = Literal[STEPS]
DecisionKind = Literal[DECISION_KINDS]
CastCost = Literal[CAST_COSTS]
SelectorWords = Annotated[list[NonBlank], Field(max_length=MAX_SELECTOR_WORDS)]
Ties = Literal[TIES]
#: `searches_for_any` (O3): the five basic land types alone, so a word
#: outside them (a mechanic, not a land type) is `malformed-play` rather than
#: silently matching nothing, the way `colors_any` does with an unknown word.
SearchesForWords = Annotated[
    list[Literal[BASIC_LAND_TYPES]], Field(max_length=len(BASIC_LAND_TYPES))
]
#: `enters_any` (O4): `hypothetical.EntersState`'s three values, by name -
#: spelled out here rather than imported so this schema stays free of the
#: engine's `machines.hypothetical` module, which only the server-side
#: evaluation path (`hypothetical_facts.py`) touches. A word outside the
#: three is `malformed-play`, the same closed-vocabulary treatment
#: `searches_for_any` gets.
ENTERS_STATES = ("untapped", "tapped", "untapped_if_paid")
EntersAnyWords = Annotated[list[Literal[ENTERS_STATES]], Field(max_length=len(ENTERS_STATES))]
#: `payable_any` (T5): how soon the engine says this card's cost can actually
#: be paid, in colours as well as amount - `now` with the mana available this
#: moment, `with_a_land` once the land drop still available this turn is made,
#: `next_turn` once the lands already on the battlefield untap, and `no` when
#: none of those pays for it. Spelled out here rather than imported for the
#: same reason `enters_any` is: this schema stays free of the engine's
#: `machines.hypothetical`, which only the server-side evaluation path
#: (`hypothetical_facts.py`) touches. A word outside the four is
#: `malformed-play`, never a silent no-match.
PAYABLE_STATES = ("now", "with_a_land", "next_turn", "no")
PayableAnyWords = Annotated[list[Literal[PAYABLE_STATES]], Field(max_length=len(PAYABLE_STATES))]


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


class MissingFrom(_Strict):
    """`{zones, side, by}`: the candidate matches only when no card on `side`
    in `zones` shares what `by` compares with it - its subtypes (those the
    selector's own `subtypes_any` lists, when it lists any) or its name. A
    comparison between objects, never a name written in the script."""

    zones: list[Literal[MISSING_FROM_ZONES]] = Field(
        min_length=1, max_length=len(MISSING_FROM_ZONES)
    )
    side: Literal[MISSING_FROM_SIDES]
    by: Literal[MISSING_FROM_BY]


class Affordable(_Strict):
    """`{extra: n}` (O7): mana value <= untapped mana + `extra`. `extra` 0
    asks whether the card is affordable with untapped mana alone."""

    extra: AtLeastZero


SELECTOR_WORD_FIELDS = (
    "types_all",
    "types_any",
    "subtypes_any",
    "supertypes_any",
    "colors_any",
    "keywords_any",
)


class Selector(_Strict):
    """Characteristics of one card, all optional, combined with AND. No field
    takes a card name: type words come from the type line, text fields are
    template phrases matched case-insensitively, and `missing_from` compares
    the candidate with the cards already in some zones."""

    types_all: SelectorWords = None
    types_any: SelectorWords = None
    subtypes_any: SelectorWords = None
    supertypes_any: SelectorWords = None
    colors_any: SelectorWords = None
    mana_value: Comparison = None
    power: Comparison = None
    toughness: Comparison = None
    keywords_any: SelectorWords = None
    tapped: bool = None
    counters: CounterComparison = None
    ability_text_has: NonBlank = None
    oracle_has: NonBlank = None
    searches_for_any: SearchesForWords = None
    land_face: bool = None
    missing_from: MissingFrom = None
    #: How this land would enter the battlefield right now (O4), judged
    #: against the live game - see `hypothetical_facts.py`.
    enters_any: EntersAnyWords = None
    #: How soon this card's cost can be paid, colours included, judged
    #: against the live game (T5) - see `hypothetical_facts.py`. A card the
    #: engine answers no payability for (an X cost) carries no fact and
    #: matches nothing, whichever words are asked for.
    payable_any: PayableAnyWords = None
    #: With this land hypothetically on the battlefield, some other hand
    #: card becomes payable that is not payable now (O7).
    unlocks: bool = None
    #: Same as `unlocks`, one turn further out: with this land on the
    #: battlefield and the lands already there untapped, some other hand card
    #: becomes payable that is not payable next turn without it (T5).
    unlocks_next: bool = None
    #: This hand card searches the library, and some library land it could
    #: find would `unlocks` (O7).
    unlocks_by_search: bool = None
    #: Same as `unlocks_by_search`, narrowed to a *basic* land (supertype
    #: `Basic`) the search could find - a fetch-conservation play that only
    #: wants "free" basics reachable, never a shockland or other non-basic
    #: unlock (O7).
    unlocks_by_basic_search: bool = None
    #: Mana value <= untapped mana + `extra` (O7). DEPRECATED, replaced by
    #: `payable_any`: `affordable` is colour-blind, because it compares the
    #: card's plain `mana_value` against a count of untapped mana and so reads
    #: a double-black two-drop as affordable off two white sources.
    #: `payable_any` asks the engine whether the cost can actually be paid,
    #: colours included. Kept, and still answered exactly as before, so no
    #: script already written stops validating.
    affordable: Affordable = None


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


class TurnCondition(_Strict):
    """The viewer's own `own_turns_taken` op `n`: how many of their own
    turns they have begun (`TurnMachine.own_turns_taken`, CR 500.7 extra
    turns included), so it reads my Nth turn on the play and on the draw
    alike, and an extra turn is its own turn - unlike `turn_number` (rounds
    of the table), it moves for one. 0 before the viewer's first turn,
    pregame included - never null."""

    kind: Literal["turn"]
    op: Op
    n: AtLeastZero


class InputCondition(_Strict):
    """A fact only the agent knows: a declared input or `opposing_archetype`."""

    kind: Literal["input"]
    name: NonBlank
    op: Literal[INPUT_OPS]
    value: Any

    @field_validator("value")
    @classmethod
    def _value_list_is_bounded(cls, value: Any) -> Any:
        if isinstance(value, list) and len(value) > MAX_INPUT_VALUES:
            raise PydanticCustomError(
                "too_many_input_values",
                "a value list holds at most {max} values, not {count}",
                {"max": MAX_INPUT_VALUES, "count": len(value)},
            )
        return value


Condition = Annotated[
    CountCondition
    | ZoneSizeCondition
    | LifeCondition
    | StackTopCondition
    | LandDropCondition
    | UntappedManaCondition
    | TurnCondition
    | InputCondition,
    Field(discriminator="kind"),
]


# -- moves -------------------------------------------------------------------

#: Ordered tie-breakers, shared by `Pick`, `SacrificeChoice`, `LandCard` and
#: `ZonedCard`.
Prefer = Annotated[list[Selector], Field(max_length=MAX_PREFER)]


class Pick(_Strict):
    select: Selector
    count: Annotated[int, Field(ge=1)] = None
    prefer: Prefer = None
    ties: Ties = None


def _amount_tag(value: Any) -> str:
    return (
        "amount-count"
        if isinstance(value, int) and not isinstance(value, bool)
        else "amount-policy"
    )


#: How much to pay on a pay-amount decision: a count from 0, or a policy.
Amount = Annotated[
    Annotated[AtLeastZero, Tag("amount-count")]
    | Annotated[Literal[AMOUNT_POLICIES], Tag("amount-policy")],
    Discriminator(_amount_tag),
]


def answer_shape_problem(answer: Any) -> str | None:
    """Why this answer item's fields do not fit its decision kind, or None.

    A pay-amount decision (`AMOUNT_DECISION_KINDS`) is answered with `amount`
    and nothing else; no other decision kind takes an `amount`. `choice`
    (O6) is answered only by `CHOICE_DECISION_KINDS`
    (`choose_a_mode`/`choose_a_quality`); everywhere else it is
    `answer-shape-mismatch`, the same footing `amount` is on. A decision
    outside `DECISION_KINDS` is judged by `unknown-decision-kind` instead."""
    if not isinstance(answer, dict) or answer.get("decision") not in DECISION_KINDS:
        return None
    decision = answer["decision"]
    if decision in AMOUNT_DECISION_KINDS:
        others = [field for field in NON_AMOUNT_ANSWER_FIELDS if field in answer]
        if others:
            return f"'{decision}' is answered with an amount, not {', '.join(others)}"
        if "amount" not in answer:
            return f"'{decision}' is answered with an amount; name one"
        return None
    if "amount" in answer:
        return f"only {', '.join(AMOUNT_DECISION_KINDS)} take an amount, not '{decision}'"
    if "choice" in answer and decision not in CHOICE_DECISION_KINDS:
        return f"only {', '.join(CHOICE_DECISION_KINDS)} take a choice, not '{decision}'"
    return None


class Answer(_Strict):
    """How to answer one pending decision of kind `decision`. A pay-amount
    decision takes `amount` alone; `choice` (O6) is taken only by
    `CHOICE_DECISION_KINDS`; every other kind takes `pick`, `accept`,
    `to_graveyard`, `to_bottom`, `pile_one` or `players`."""

    decision: DecisionKind
    pick: Pick = None
    accept: bool = None
    to_graveyard: Selector = None
    to_bottom: Selector = None
    amount: Amount = None
    choice: NonBlank = None
    #: `choose_pile`'s own answer (CR 700.3's pile primitive, Liliana of the
    #: Veil's own `-6`, Do or Die) - `True` for pile one
    #: (`pending_decision.cards`), `False` for its complement. Not `accept`,
    #: the same reason the api's own wire shape keeps it separate: a two-way
    #: pile choice is not a yes/no.
    pile_one: bool = None
    #: `choose_players`'s own answer (CR 701.34a's own Proliferate player
    #: half) - which side(s) to choose among `pending_decision.players`,
    #: resolved to real usernames by `runtime.decide_body` the same way a
    #: `PlayerTarget`'s `player` field already is. Not a `Selector`: a player
    #: has no characteristics a selector could match against.
    players: list[Literal["me", "opponent"]] = Field(default=None, max_length=MAX_PLAYERS_ANSWER)

    @model_validator(mode="after")
    def _fields_fit_the_decision(self) -> Answer:
        problem = answer_shape_problem(
            {**{name: True for name in self.model_fields_set}, "decision": self.decision}
        )
        if problem is not None:
            raise PydanticCustomError("answer_shape_mismatch", problem)
        return self

    @field_validator("decision")
    @classmethod
    def _not_a_combat_declaration_or_mulligan(cls, decision: str) -> str:
        if decision in FORBIDDEN_DECISION_KINDS:
            raise PydanticCustomError(
                "forbidden_decision",
                "'{decision}' is not answered by a script; "
                "scripts do not attack, block, assign combat damage or mulligan",
                {"decision": decision},
            )
        return decision


class ZonedCard(_Strict):
    zone: Literal[COUNT_ZONES]
    select: Selector
    prefer: Prefer = None
    ties: Ties = None


class CardTarget(_Strict):
    """A card or permanent target: what `select` matches, on `side`."""

    select: Selector
    side: Side


class PlayerTarget(_Strict):
    """A player target: the viewer or an opponent."""

    player: Literal["me", "opponent"]


def _target_tag(value: Any) -> str:
    return "player-target" if isinstance(value, dict) and "player" in value else "card-target"


#: One target a cast or an activation announces, in order.
Target = Annotated[
    Annotated[CardTarget, Tag("card-target")] | Annotated[PlayerTarget, Tag("player-target")],
    Discriminator(_target_tag),
]


class LandCard(_Strict):
    select: Selector
    prefer: Prefer = None
    ties: Ties = None


class SacrificeChoice(Pick):
    """What an activation cost's own "Sacrifice a ..." piece sacrifices:
    permanents the activating player controls matching `select`, `count` of
    them (the cost's own number when left out), best first by `prefer`."""


def _sacrifice_tag(value: Any) -> str:
    return "sacrifice-choice" if isinstance(value, dict) else "sacrifice-keyword"


#: `source` sacrifices the ability's own source, `none` nothing; an object
#: chooses what to sacrifice by selector.
Sacrifice = Annotated[
    Annotated[Literal["source", "none"], Tag("sacrifice-keyword")]
    | Annotated[SacrificeChoice, Tag("sacrifice-choice")],
    Discriminator(_sacrifice_tag),
]


class DiscardChoice(Pick):
    """What an activation cost's own "Discard a/N card(s)" piece discards
    (CR 701.9): cards from the activating player's own HAND matching
    `select`, `count` of them (the cost's own number when left out), best
    first by `prefer`. Never the ability's own source - an off-zone
    activation whose cost is "Discard this card" (CR 702.29a cycling,
    CR 702.53a transmute) has nothing here to name at all, since the source
    already sits in hand and the cost spells the whole thing out; that case
    is `sacrifice`'s own "source" value, read the same way regardless of
    whether the underlying CR 118.3 component is a sacrifice or a discard."""


def _discard_tag(value: Any) -> str:
    return "discard-choice" if isinstance(value, dict) else "discard-keyword"


#: `none` discards nothing; an object chooses what to discard by selector.
#: No "source" keyword here - unlike `sacrifice`, which already carries that
#: meaning for a battlefield ability's own source and for an off-zone
#: ability's "Discard this card" self-reference alike.
Discard = Annotated[
    Annotated[Literal["none"], Tag("discard-keyword")]
    | Annotated[DiscardChoice, Tag("discard-choice")],
    Discriminator(_discard_tag),
]


class ActivateMove(_Strict):
    kind: Literal["activate"]
    source: ZonedCard
    ability_text_has: NonBlank
    targets: list[Target] = Field(default_factory=list, max_length=MAX_TARGETS)
    sacrifice: Sacrifice = None
    discard: Discard = None
    answers: list[Answer] = Field(default_factory=list, max_length=MAX_ANSWERS)


class CastMove(_Strict):
    kind: Literal["cast"]
    card: ZonedCard
    cost: CastCost = None
    targets: list[Target] = Field(default_factory=list, max_length=MAX_TARGETS)
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

    @field_validator("steps")
    @classmethod
    def _each_step_once(cls, steps: list[str]) -> list[str]:
        repeated = sorted({step for step in steps if steps.count(step) > 1})
        if repeated:
            raise PydanticCustomError(
                "duplicate_step",
                "names {steps} more than once",
                {"steps": ", ".join(repeated)},
            )
        return steps


class Play(_Strict):
    """One play script. `name` and `intent` are prose nothing executes;
    `note` is prose too, but for a different reader - `intent` is written for
    a human reading a diff, `note` is written for an agent reading it
    mid-game, saying why this play is a good choice right now. `priority` 0
    disables it; `true` must all hold and `false` must all not hold; `next`
    names plays of the same deck tried after the moves. A play with no moves
    is advice-only: it commits nothing to the game, and exists to explain a
    decision rather than to act on one."""

    schema_version: Literal[1]
    id: NonBlank
    name: NonBlank
    intent: NonBlank = None
    note: Note = None
    priority: Annotated[int, Field(ge=0, le=MAX_PRIORITY)]
    opposing_archetypes: list[ArchetypeKey] = Field(min_length=1, max_length=MAX_ARCHETYPES)
    when: When
    true: list[Condition] = Field(default_factory=list, max_length=MAX_CONDITIONS)
    false: list[Condition] = Field(default_factory=list, max_length=MAX_CONDITIONS)
    moves: list[Move] = Field(default_factory=list, max_length=MAX_MOVES)
    next: list[NonBlank] = Field(default_factory=list, max_length=MAX_NEXT)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version_is_not_a_bool(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise PydanticCustomError("literal_error", "Input should be 1")
        return value

    @field_validator("opposing_archetypes")
    @classmethod
    def _wildcard_stands_alone(cls, keys: list[str]) -> list[str]:
        if WILDCARD in keys and len(keys) > 1:
            raise PydanticCustomError(
                "wildcard_with_keys",
                "'{wildcard}' already means every archetype; name it alone or name keys, not both",
                {"wildcard": WILDCARD},
            )
        return keys


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
    "targets": TypeAdapter(Target),
    "prefer": TypeAdapter(Selector),
    "opposing_archetypes": TypeAdapter(ArchetypeKey),
    "zones": TypeAdapter(Literal[MISSING_FROM_ZONES]),
    "searches_for_any": TypeAdapter(Literal[BASIC_LAND_TYPES]),
    "enters_any": TypeAdapter(Literal[ENTERS_STATES]),
    "payable_any": TypeAdapter(Literal[PAYABLE_STATES]),
    **{field: TypeAdapter(NonBlank) for field in SELECTOR_WORD_FIELDS},
}
_BOUND_TYPES = (
    "too_long",
    "string_too_long",
    "too_many_input_values",
    "greater_than_equal",
    "less_than_equal",
    "greater_than",
    "less_than",
)
#: Pydantic's own error type for a model's custom check, by the code it is.
_CUSTOM_CODES = {
    "hidden_zone_side_any": "hidden-zone-side-any",
    "forbidden_decision": "forbidden-kind",
    "duplicate_step": "duplicate-step",
    "wildcard_with_keys": "wildcard-with-keys",
    "answer_shape_mismatch": "answer-shape-mismatch",
}
#: A value outside a closed Literal, by the field it is in.
_LITERAL_CODES = {"decision": "unknown-decision-kind", "cost": "unknown-cast-cost"}
#: The tags of the unions discriminated by shape rather than by `kind`. They
#: hold a hyphen, so no key of a document is ever spelled like one.
_SHAPE_TAGS = frozenset(
    {"amount-count", "amount-policy", "card-target", "player-target"}
    | {"sacrifice-keyword", "sacrifice-choice"}
    | {"discard-keyword", "discard-choice"}
)
_MISSING = object()


def _walk(doc: Any, loc: tuple[Any, ...]) -> tuple[str, Any]:
    """The dotted location of `loc` in `doc`, and the value found there.

    Pydantic puts a discriminated union's tag into the location: after the
    list index for a `kind` union, after the field for a shape union
    (`_SHAPE_TAGS`). A tag names no key of the document, so it is skipped."""
    text, node = "", doc
    for part in loc:
        if part in _SHAPE_TAGS or (
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
    return next(
        (part for part in reversed(loc) if isinstance(part, str) and part not in _SHAPE_TAGS),
        None,
    )


def _code(error: dict[str, Any], loc: tuple[Any, ...]) -> str:
    kind = error["type"]
    if kind == "extra_forbidden":
        return "unknown-field"
    if kind in _CUSTOM_CODES:
        return _CUSTOM_CODES[kind]
    if kind == "literal_error" and _last_field(loc) in _LITERAL_CODES:
        return _LITERAL_CODES[_last_field(loc)]
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
    if code == "forbidden-kind" and error["type"] == "union_tag_invalid":
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


def _answer_shape_mismatches(doc: Any) -> Iterator[tuple[str, str, str]]:
    """Every answer item whose fields do not fit its decision kind, whatever
    else is wrong with it - pydantic runs `Answer`'s own check only once every
    field of the answer validated."""
    if not isinstance(doc, dict) or not isinstance(doc.get("moves"), list):
        return
    for move_index, move in enumerate(doc["moves"]):
        answers = move.get("answers") if isinstance(move, dict) else None
        if not isinstance(answers, list):
            continue
        for answer_index, answer in enumerate(answers):
            problem = answer_shape_problem(answer)
            if problem is not None:
                where = f"moves[{move_index}].answers[{answer_index}]"
                yield "answer-shape-mismatch", where, problem


def play_errors(doc: Any, path: str = "play") -> list[PlaysError]:
    """Every structural refusal of one play document, as coded errors."""
    found: list[tuple[str, str, str]] = []
    try:
        Play.model_validate(doc)
    except ValidationError as exc:
        found.extend(_translate(exc.errors(), doc, ()))
    found.extend(_hidden_zone_side_any(doc))
    found.extend(_answer_shape_mismatches(doc))
    reported: set[tuple[str, str]] = set()
    errors = []
    for code, where, message in found:
        if (code, where) in reported:
            continue
        reported.add((code, where))
        errors.append(PlaysError(code, path, f"{where}: {message}" if where else message))
    return errors
