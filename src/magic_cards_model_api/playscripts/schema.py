"""The play-script schema: design D2, closed and bounded.

`decks/<slug>/plays/<id>.json` is a declarative chain a seat runs through the
seatkit runner. `Play` is its model: every object forbids unknown keys, every
union is discriminated on `kind` (or, for a `compare` operand, `of`), every
list is bounded (`maxItems` in the exported JSON Schema, an explicit bound on
every list field below), and no field or kind can iterate or concede. The
vocabulary is closed at eleven condition kinds and nine move kinds (FD2 adds
`keyword_action` and `mulligan` to FB2's seven); a `cast` names its cost from
`CAST_COSTS` (the engine's own cast option kinds), and an answer names its
decision from `DECISION_KINDS` (the api's pending decision kinds - the two
combat declarations are answered by their own move kinds instead,
`declare_attackers`/`declare_blockers`, since FB2's B4).
`when.steps` names each step once, including the two combat declaration steps
(FB2 W1), and `opposing_archetypes` is either `["*"]` or named keys, never
both. An answer's fields fit its decision kind: a pay-amount decision
(`AMOUNT_DECISION_KINDS`) is answered with `amount` alone, `top_order` fits
only `scry`/`surveil`, `position` only `put_on_top_or_bottom`, `targets` only
`choose_targets`, and no other kind takes one (`answer-shape-mismatch`). A
`cast` or `activate` target is a card by selector and side (optionally
narrowed by `zone` and ranked by `prefer`/`ties`), or a player; an `activate`
sacrifices its source, nothing, or permanents chosen by selector, and
independently discards nothing or hand cards chosen by selector (never the
source itself - that is `sacrifice`'s own "source" value; CR 701.9's discard
always reads from hand, never the battlefield); it names its ability by
`ability_text_has` or `ability_index`, exactly one (`activate-ability-ref`). A
selector's optional `missing_from` names zones (`MISSING_FROM_ZONES`, at
least one), a side (`MISSING_FROM_SIDES`) and what it compares
(`MISSING_FROM_BY`); a word outside those is `malformed-play` and no zones
`bounds-exceeded`, so it adds no code.

FB3's FD2 additions (2026-09-26), all additive over the shipped v1
vocabulary and over FB2's B4: `cast` gains `face` (a face selector for a
split/Adventure/MDFC spell face), `modes`, `x`, `shares`, `optional_costs`,
cost-object `sacrifice`/`discard`/`exile`/`tap` Picks and
`convoke`/`delve`/`improvise` Picks; `activate` gains `exile`, `x` and
`color`; two new move kinds, `keyword_action` (suspend, plot, foretell,
cycle, equip, crew, turn_face_up, companion) and `mulligan` (valid only in
the `mulligan` step); three schema-local steps (`cleanup`, `mulligan`,
`pregame_actions`); an answer gains `order` (order_triggers), `damage`
(assign_combat_damage) and `choice_from` (choose_a_quality, a selector
instead of a literal string); `pick.count` and `targets` may now be 0
("choose none"); and `FORBIDDEN_DECISION_KINDS` is empty - `mulligan` is
answered like `may`/`pay_life` (the plain `accept` field) or, more fully,
through the new `mulligan` move, and `assign_combat_damage` is answered
only through the new `damage` field. `concede` was never in the vocabulary
and still is not: it is not a `DecisionKind` the engine ever pends, and no
move kind is ever named `concede`.

FD9's additions (2026-09-26), also additive, still `schema_version` 1: a
tenth move kind, `repeat {moves, until, max_iterations, stop_when_refused?}`
(owner: "DSL is meant to fix missing repeat-until loops"). `moves` is 1-3 of
the existing move kinds, none of them `repeat` (`repeat-nested`) or `pass`
(`repeat-pass-in-body`) - a loop's own body commits real moves and exits
through `until`, `max_iterations` or a refusal, never through releasing
priority. `until` is 1-8 conditions of the existing kinds (an empty list is
`repeat-empty-until`), judged fresh against a freshly read board before every
iteration, so an `until` that already holds runs the loop zero times.
`max_iterations` is required, 1-40 (missing or out of range is
`repeat-bound`) - the loop's own hard stop, independent of whatever `until`
alone would leave unbounded. `stop_when_refused` (default false) turns an
engine refusal mid-loop into a normal end of the loop rather than a run
failure. A `repeat` counts as one move toward `MAX_MOVES`/`MAX_MOVES_PER_RUN`;
its own iterations are bounded only by `max_iterations`. A twelfth condition
kind, `mana_pool` (and matching `compare` operand), reads the viewer's own
floating mana pool - `untapped_mana` counts untapped SOURCES, never the pool
itself, so it cannot express a loop's own mana target (the Devoted Druid +
Vizier of Remedies loop, finding seq 191, needed roughly 25 manual MCP calls
for exactly this reason). The runtime that actually executes a `repeat` is
FD3 part 7's, in `playscripts/runtime.py`; this schema only shapes and lints
the construct.

CR 709.5e's addition (2026-09-27), additive, still `schema_version` 1: a
ninth `KEYWORD_ACTIONS` entry, `unlock_door` (pay the mana cost of a locked
half - "door" - of a Room permanent you control to unlock it). `Keyword
ActionMove` gains `door` (the locked half's own printed name, e.g. "Ritual
Chamber"), meaningful only for this one action - every existing action
leaves it out, exactly as before.

CR 702.87a's and CR 702.151a's addition (2026-10-07), additive, still
`schema_version` 1: two more `KEYWORD_ACTIONS` entries over two new routes,
`level_up` (put a level counter on a leveler - names only its `card`) and
`reconfigure` (attach an Equipment with reconfigure to the `target` creature
you control, or, with no `target`, unattach it). No new field: `reconfigure`
reuses equip's `target`.

FB2's B4 additions (2026-09-24), all additive over the shipped v1 vocabulary:
`pass` (CR 117.3d, must be the play's last move - `pass-not-last`),
`declare_attackers`/`declare_blockers` (CR 508.1/509.1, must be the play's
first move, at most one per play - `combat-move-not-first`), `ability_index`
on `activate`, `top_order`/`position` on an answer item, `targets` on a
`choose_targets` answer (so a player target is expressible there too),
`prefer`/`ties`/`zone` on a `CardTarget`, and three condition kinds:
`pending_decision` (a decision of that kind is pending for the viewer),
`compare` (two board- or literal-read `Operand`s against each other, so
"their board's power beats my life" is expressible without a bespoke
condition), and `all_of` (allowed only inside `false`, meaning "not (A and
B ...)"; a member may not itself be `all_of` - nesting is refused). Every one
of these is a new optional field or a new discriminated `kind`/`of` value, so
every play already committed - the 94 in the mtg-decks corpus at the time of
writing - stays valid unchanged, and `schema_version` stays 1: a strict union
already refuses an unrecognized `kind` loudly in a reader built before this
change, rather than silently misreading it, so the only real consequence is
deploy ordering - this api (which can now READ a script naming one of these
new kinds) has to reach main and a live deploy before any deck's plays
actually use one, or an older reader (or the mtg-decks stdlib mirror,
`check_plays.py`, before its own FB2 update lands) would refuse a perfectly
valid v1 file it does not yet recognize.

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
#: `top_order`'s own fan-out (FB2 A1): tiers ordering the cards kept on top
#: after a scry/surveil, first tier on top - the same fan-out `prefer` gets.
MAX_TOP_ORDER = 4
#: `ActivateMove.ability_index` (FB2 M4): 0-15, the same footing a card
#: view's own `abilities` list is indexed on - a card with more activated
#: abilities than that is named by `ability_text_has` instead.
MAX_ABILITY_INDEX = 15
#: `DeclareAttackersMove.attackers` (FB2 M2): one entry per group of
#: creatures sharing a defender, the same fan-out `blocks` gets.
MAX_ATTACK_GROUPS = 6
#: `DeclareBlockersMove.blocks` (FB2 M3): one entry per blocker-to-attacker
#: assignment.
MAX_BLOCKS = 6
#: `AllOfCondition.conditions` (FB2 C3): 2-4 leaf conditions - fewer than two
#: is not a conjunction at all, and the fan-out tops out with the rest of the
#: schema's small lists.
MIN_ALL_OF_CONDITIONS = 2
MAX_ALL_OF_CONDITIONS = 4
#: `CastMove.modes` (FD2): CR 700.2a's modal spells offer at most a handful of
#: modes in practice; 5 is comfortably above the widest printed count.
MAX_MODES = 5
#: `CastMove.optional_costs` (FD2): one entry per optional cost the api's own
#: `optional_costs` accepts (kicker, multikicker, replicate, offspring,
#: blight, gift, entwine, escalate, harmonize, pay life, bargain, collect
#: evidence, reveal) - 4 is comfortably above how many one printed spell ever
#: stacks.
MAX_OPTIONAL_COSTS = 4
#: `Answer.order` (FD2): one entry per simultaneously triggered ability a
#: seat orders (CR 603.3b) - the same fan-out `AttackGroup`/`Block` get.
MAX_ORDER = 6
#: `CastMove.x`/`ActivateMove.x` (FD2): CR 107.3's X, bounded well above any
#: printed or reasonably paid value.
MAX_X = 30
#: `Answer.damage`'s own `tiers` (FD2): one entry per ordered blocker tier,
#: the same fan-out `DeclareBlockersMove.blocks` gets.
MAX_DAMAGE_TIERS = MAX_BLOCKS
#: `RepeatMove.moves` (FD9): a loop's own body - small on purpose, the same
#: reasoning `MAX_MOVES` already gives a play: a line long enough to need more
#: than a handful of steps per iteration is a script design smell, not a
#: bound this construct should relax.
MAX_REPEAT_BODY = 3
#: `RepeatMove.max_iterations` (FD9): the loop's own hard stop, comfortably
#: above any printed mana-loop or ping-down target while still finite - the
#: bound that keeps a runaway `until` from ever looping forever.
MAX_REPEAT_ITERATIONS = 40
#: `ActivateMove.color` (FD2): the six colour letters `ActivateRequest.color`
#: accepts.
COLOR_LETTERS_CLOSED = ("W", "U", "B", "R", "G", "C")
#: `KeywordActionMove.action` (FD2): the six existing keyword-action routes
#: (suspend, plot, foretell, cycle, equip, crew) plus FD5's two new ones
#: (turn_face_up, companion) plus CR 709.5e's `unlock_door`, CR 702.184a's
#: `station`, CR 702.87a's `level_up` and CR 702.151a's `reconfigure`.
KEYWORD_ACTIONS = (
    "suspend",
    "plot",
    "foretell",
    "cycle",
    "equip",
    "crew",
    "turn_face_up",
    "companion",
    "unlock_door",
    "station",
    "level_up",
    "reconfigure",
)
#: `Answer.damage`'s own `rest_to` (FD2): where damage past the last lethal
#: blocker tier goes once trample lets it through - the defending player, or
#: piled onto the last blocker tier instead.
REST_TO_VALUES = ("player", "last_blocker")
#: The decision kind `Answer.damage` (FD2) fits, and only it.
DAMAGE_DECISION_KINDS = ("assign_combat_damage",)
#: The decision kind `Answer.order` (FD2) fits, and only it.
ORDER_DECISION_KINDS = ("order_triggers",)
#: The decision kind `Answer.choice_from` (FD2) fits, and only it -
#: `choose_a_mode` is always fully enumerated by fixed mode text, never a card.
CHOICE_FROM_DECISION_KINDS = ("choose_a_quality",)
#: FD2 item 5: `when.steps` accepts every step in which a decision can be
#: pending, not only the steps a `pass_until` run can stop at
#: (`AUTOPASS_STEPS`). `cleanup` (CR 514.2/514.3's own discard-to-hand-size
#: and triggered-ability priority window), `mulligan` (CR 103.5) and
#: `pregame_actions` (CR 103.6) are schema-local additions layered on top of
#: `seatkit.AUTOPASS_STEPS`, which is never edited: no `pass_until` run can
#: ever stop at any of the three (CR 502.4/514.3 give no player priority in
#: an ordinary untap or cleanup step, and neither pregame step is in the
#: engine's `TURN_ORDER` at all), but a script can still be scoped to run
#: while one of them holds the board.
SCHEMA_ONLY_STEPS = ("cleanup", "mulligan", "pregame_actions")

STEPS = AUTOPASS_STEPS + SCHEMA_ONLY_STEPS
CONDITION_KINDS = (
    "count",
    "zone_size",
    "life",
    "stack_top",
    "land_drop_available",
    "untapped_mana",
    "mana_pool",
    "turn",
    "input",
    "pending_decision",
    "compare",
    "all_of",
)
MOVE_KINDS = (
    "activate",
    "cast",
    "play_land",
    "answer",
    "pass",
    "declare_attackers",
    "declare_blockers",
    "keyword_action",
    "mulligan",
    "repeat",
)
#: `compare`'s own `Operand.of` vocabulary (FB2 C2, FD9's `mana_pool` added) -
#: purely descriptive here, the way `CONDITION_KINDS`/`MOVE_KINDS` are: each
#: operand class carries its own `Literal["..."]` `of` value, this tuple is
#: what the doc and drift tests pin against.
OPERAND_KINDS = (
    "count",
    "zone_size",
    "life",
    "untapped_mana",
    "mana_pool",
    "power_sum",
    "toughness_sum",
    "n",
)
#: The two decision kinds FB2 moved out of `answers[]` into their own move
#: kinds (`declare_attackers`/`declare_blockers`, M2/M3). An answer item still
#: naming one is refused with `use-combat-move`, not `forbidden-kind` -
#: `FORBIDDEN_DECISION_KINDS` below no longer includes them.
COMBAT_DECISION_KINDS = ("declare_attackers", "declare_blockers")
COUNT_ZONES = ("battlefield", "graveyard", "exile", "hand", "stack", "library", "sideboard")
SIZE_ZONES = ("hand", "library", "graveyard", "exile", "sideboard")
HIDDEN_ZONES = ("hand", "library", "sideboard")
SIDES = ("me", "opponent", "any")
#: `Selector.missing_from`: the zones it reads are a count's own, on one named
#: side (never `any`), and it compares either subtypes or card names.
MISSING_FROM_ZONES = COUNT_ZONES
MISSING_FROM_SIDES = ("me", "opponent")
MISSING_FROM_BY = ("subtype", "name")
#: `CardTarget.zone` (FB2 T1): the zones a target may be narrowed to. Left
#: out, a target still resolves over battlefield and stack, today's
#: behaviour.
TARGET_ZONES = ("battlefield", "stack", "graveyard", "exile")
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
    "awaken",
    "bestow",
    "blitz",
    "dash",
    "disguise",
    "disturb",
    "emerge",
    "escape",
    "evoke",
    "flashback",
    "foretell",
    "freerunning",
    "harmonize",
    "impending",
    "jump-start",
    "madness",
    "mayhem",
    "megamorph",
    "morph",
    "plot",
    "printed",
    "prowl",
    "retrace",
    "sneak",
    "spectacle",
    "surge",
    "warp",
    "web-slinging",
)
#: The decision kinds an answer may name: every `DecisionKind` the api can
#: pend (`decisions.ANSWERABLE_KINDS`), by value.
DECISION_KINDS = tuple(sorted(kind.value for kind in ANSWERABLE_KINDS))
#: Pending decision kinds a script never answers directly: none, as of FD2.
#: `declare_attackers`/`declare_blockers` used to be here too; FB2's B4 gives
#: them their own move kinds instead (`COMBAT_DECISION_KINDS` above), answered
#: with `use-combat-move`, not `forbidden-kind`. `assign_combat_damage` and
#: `mulligan` were here through FB2: FD2 gives the first its own `damage`
#: answer field (`DAMAGE_DECISION_KINDS`, required and exclusive there, the
#: same footing `amount` has) and lets the second answer plainly with
#: `accept`, the way `may`/`pay_life` already do, alongside the new dedicated
#: `mulligan` move for the fuller keep-or-take-with-bottom shape. Kept as a
#: named, empty tuple - not deleted - because it is still the vocabulary's own
#: place for a kind a script must never answer directly, if one is ever
#: added back.
FORBIDDEN_DECISION_KINDS = ()
#: The decision kinds answered with a number rather than yes/no or cards: the
#: kinds `decisions.ANSWER_FIELD_FOR_KIND` answers on the engine's `amount`
#: field (`pay_energy_amount`, `pay_life_amount` - an integer from 0 up to the
#: decision's own `amount`, the payer's energy or life; `choose_an_amount` -
#: that same shape with nothing paid, "put up to N counters" and any other
#: CR 608.2d "choose an amount" with no cost attached). Their answer item
#: carries `amount` and nothing else.
AMOUNT_DECISION_KINDS = tuple(
    sorted(kind.value for kind, field in ANSWER_FIELD_FOR_KIND.items() if field == "amount")
)
#: `amount: "max"` pays the most the decision allows (its own `amount`).
AMOUNT_POLICIES = ("max",)
#: The decision kinds answered by naming a string from an offered (or, for
#: `choose_a_quality`, sometimes open) list: the kinds the api's
#: `decisions.TOOL_FIELD_FOR_KIND` answers on `choice` (Cavern of Souls' and
#: Utopia Sprawl's "as this enters, choose a ..." CR 614.12a, and CR 700.2a's
#: "choose ... mode" abilities). O6.
CHOICE_DECISION_KINDS = tuple(
    sorted(kind.value for kind, field in TOOL_FIELD_FOR_KIND.items() if field == "choice")
)
#: `top_order` (FB2 A1) fits only a scry/surveil answer - the same tiers
#: shape orders what stays on top after either.
TOP_ORDER_DECISION_KINDS = ("scry", "surveil")
#: `position` (FB2 A1) fits only `put_on_top_or_bottom` - an alternative to
#: `to_bottom` for that one decision.
POSITION_DECISION_KINDS = ("put_on_top_or_bottom",)
#: `targets` (FB2 A2) fits only `choose_targets` - the alternative to `pick`
#: that can also name a player target.
TARGETS_DECISION_KINDS = ("choose_targets",)
#: The answer fields an amount decision never takes.
NON_AMOUNT_ANSWER_FIELDS = (
    "pick",
    "accept",
    "to_graveyard",
    "to_bottom",
    "choice",
    "choice_from",
    "pile_one",
    "to_hand",
    "players",
    "top_order",
    "position",
    "targets",
    "order",
    "damage",
)
#: The answer fields an `assign_combat_damage` decision never takes - the
#: same idea `NON_AMOUNT_ANSWER_FIELDS` is, `amount` included: `damage` (FD2)
#: answers it alone.
NON_DAMAGE_ANSWER_FIELDS = (
    "pick",
    "accept",
    "to_graveyard",
    "to_bottom",
    "choice",
    "choice_from",
    "pile_one",
    "players",
    "top_order",
    "position",
    "targets",
    "order",
    "amount",
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
    "pass-not-last",
    "combat-move-not-first",
    "activate-ability-ref",
    "all-of-outside-false",
    "use-combat-move",
    "answer-decision-mismatch",
    "mulligan-wrong-step",
    "repeat-nested",
    "repeat-pass-in-body",
    "repeat-bound",
    "repeat-empty-until",
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
#: `ActivateMove.ability_index` (FB2 M4): the card's own ability list index.
AbilityIndex = Annotated[int, Field(ge=0, le=MAX_ABILITY_INDEX)]
#: `CardTarget.zone` (FB2 T1).
TargetZone = Literal[TARGET_ZONES]
#: `ActivateMove.color` (FD2): the six colour letters `ActivateRequest.color`
#: accepts, for a mana ability with more than one colour to choose from.
Color = Literal[COLOR_LETTERS_CLOSED]
#: `KeywordActionMove.action` (FD2).
KeywordAction = Literal[KEYWORD_ACTIONS]
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
    selector's own `subtypes_any` also lists, when it lists any) or its name.
    A comparison between objects, never a name written in the script."""

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


class ManaPoolCondition(_Strict):
    """FD9: the viewer's own floating mana pool (`ManaPoolView.floating`,
    CR 106.4) - the mana already produced by mana abilities and not yet
    spent, on the same footing `untapped_mana` is on (always the viewer's
    own, never a `side`). `untapped_mana` counts untapped SOURCES, never this
    pool, so it cannot express a repeat-until loop's own mana target (the
    Devoted Druid + Vizier of Remedies loop, finding seq 191). `color` (FD2's
    `Color`) narrows to one coloured symbol's own count; left out, `n`
    compares the pool's total across every color, `{C}` included."""

    kind: Literal["mana_pool"]
    color: Color = None
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


class PendingDecisionCondition(_Strict):
    """Holds when a decision of this kind is pending for the viewer (FB2
    C1). The one condition kind a standalone `answer` play's `true` names to
    say "this play only runs as a direct answer to a decision already on the
    table" - see `Play._standalone_pending_answer_names_the_pending_decision`
    below, which requires the play's one `answer` move to name the same
    `decision`, or `answer-decision-mismatch`."""

    kind: Literal["pending_decision"]
    decision: DecisionKind


class CountOperand(_Strict):
    """How many cards matching `select` are in `zone` (default battlefield),
    by side - the same reading `CountCondition`'s own `count` gives, and the
    same hidden-zone side rule: a hidden `zone` must name `me` or `opponent`,
    never `any`."""

    of: Literal["count"]
    select: Selector
    zone: Literal[COUNT_ZONES] = "battlefield"
    side: Side

    @model_validator(mode="after")
    def _hidden_zone_names_a_side(self) -> CountOperand:
        if self.zone in HIDDEN_ZONES and self.side == "any":
            raise PydanticCustomError(
                "hidden_zone_side_any",
                "a count over the {zone} must name side me or opponent, not any",
                {"zone": self.zone},
            )
        return self


class ZoneSizeOperand(_Strict):
    """A zone's public card count, by side - the same reading
    `ZoneSizeCondition` gives, `side` always named (never `any`)."""

    of: Literal["zone_size"]
    zone: Literal[SIZE_ZONES]
    side: Literal["me", "opponent"]


class LifeOperand(_Strict):
    of: Literal["life"]
    side: Literal["me", "opponent"]


class UntappedManaOperand(_Strict):
    of: Literal["untapped_mana"]
    side: Literal["me", "opponent"]


class ManaPoolOperand(_Strict):
    """`compare`'s own `mana_pool` operand (FD9): the same read
    `ManaPoolCondition` gives, on a named `side` - `untapped_mana`'s own
    operand footing (`UntappedManaOperand`)."""

    of: Literal["mana_pool"]
    side: Literal["me", "opponent"]
    color: Color = None


class PowerSumOperand(_Strict):
    """The summed power of `side`'s battlefield permanents matching `select`
    - covers "their board's power beats my life" without a bespoke condition
    kind of its own."""

    of: Literal["power_sum"]
    select: Selector
    side: Side


class ToughnessSumOperand(_Strict):
    """The summed toughness of `side`'s battlefield permanents matching
    `select` - the same reading `power_sum` gives, for toughness."""

    of: Literal["toughness_sum"]
    select: Selector
    side: Side


class NOperand(_Strict):
    """A plain literal number - the side of a `compare` that is not read off
    the board at all."""

    of: Literal["n"]
    n: int


#: `compare`'s own operand (FB2 C2), discriminated on `of` rather than
#: `kind` - every board-reading half a `count`/`zone_size`/`life`/
#: `untapped_mana` condition already reads on its own, plus the two sums a
#: bespoke "board power vs life" condition would otherwise need, plus a
#: plain literal.
Operand = Annotated[
    CountOperand
    | ZoneSizeOperand
    | LifeOperand
    | UntappedManaOperand
    | ManaPoolOperand
    | PowerSumOperand
    | ToughnessSumOperand
    | NOperand,
    Field(discriminator="of"),
]


class CompareCondition(_Strict):
    """`left op right`, each side an `Operand` read off the board or a
    literal `n` (FB2 C2) - `count`/`zone_size`/`life`/`untapped_mana` already
    compare one board read against a literal `n` on their own; `compare` puts
    the board on both sides (a deck's own creature count against the
    opponent's, or - the case that motivates it - the summed power of one
    side's board against the other's life total)."""

    kind: Literal["compare"]
    left: Operand
    op: Op
    right: Operand


#: Every condition kind except `all_of` itself - what `AllOfCondition.conditions`
#: draws from, so a member naming `all_of` fails as an unrecognized kind for
#: that list rather than nesting (FB2 C3).
LeafCondition = Annotated[
    CountCondition
    | ZoneSizeCondition
    | LifeCondition
    | StackTopCondition
    | LandDropCondition
    | UntappedManaCondition
    | ManaPoolCondition
    | TurnCondition
    | InputCondition
    | PendingDecisionCondition
    | CompareCondition,
    Field(discriminator="kind"),
]


class AllOfCondition(_Strict):
    """ "not (A and B ...)": every member must hold for `all_of` itself to
    hold (FB2 C3). Allowed only inside a play's `false` list -
    `Play._all_of_only_in_false` below refuses one in `true` with
    `all-of-outside-false` - and never nested: a member drawn from
    `LeafCondition` may not itself be `all_of`."""

    kind: Literal["all_of"]
    conditions: list[LeafCondition] = Field(
        min_length=MIN_ALL_OF_CONDITIONS, max_length=MAX_ALL_OF_CONDITIONS
    )


Condition = Annotated[
    CountCondition
    | ZoneSizeCondition
    | LifeCondition
    | StackTopCondition
    | LandDropCondition
    | UntappedManaCondition
    | ManaPoolCondition
    | TurnCondition
    | InputCondition
    | PendingDecisionCondition
    | CompareCondition
    | AllOfCondition,
    Field(discriminator="kind"),
]


# -- moves -------------------------------------------------------------------

#: Ordered tie-breakers, shared by `Pick`, `SacrificeChoice`, `LandCard`,
#: `ZonedCard`, `AttackGroup`, `BlockerPick` and `AttackerPick`.
Prefer = Annotated[list[Selector], Field(max_length=MAX_PREFER)]
#: `Answer.top_order` (FB2 A1): tiers ordering the cards kept on top after a
#: scry/surveil, first tier on top; the rest stay on top after them in
#: looked-at order. The same fan-out `Prefer` gets.
TopOrder = Annotated[list[Selector], Field(max_length=MAX_TOP_ORDER)]
#: `Answer.order` (FD2, CR 603.3b): 1-6 selector tiers ordering pending
#: triggered abilities - see `Answer.order`'s own docstring for the direction.
Order = Annotated[list[Selector], Field(min_length=1, max_length=MAX_ORDER)]


class ChoiceFromCard(_Strict):
    """`Answer.choice_from` (FD2): a card resolved at run time by selector,
    over `zone` on `side` - never a name written in the file. The runtime
    derives `choose_a_quality`'s own answer text from whichever card this
    selector matches (Cavern of Souls' creature type, say, read off the
    matched creature)."""

    zone: Literal[COUNT_ZONES]
    side: Side
    select: Selector
    prefer: Prefer = None
    ties: Ties = None


class DamagePolicy(_Strict):
    """`Answer.damage` (FD2, CR 510.1c/702.19b): a policy rather than a
    literal amount per recipient. `tiers` orders the blockers (and, once
    trample lets damage through, the defending player or planeswalker) from
    first to last in `pending_decision.targets`' own damage-assignment order;
    `lethal_first` assigns each tier only its lethal damage before any moves
    past it (CR 510.1c's own order, the default when left out); `rest_to`
    says where damage a trampling attacker has left over after every blocker
    tier has lethal damage goes - `player` past the last blocker (CR
    702.19b, the default when left out), or `last_blocker` to pile the rest
    onto the last blocker tier instead of letting it through."""

    tiers: list[Selector] = Field(default_factory=list, max_length=MAX_DAMAGE_TIERS)
    lethal_first: bool = None
    rest_to: Literal[REST_TO_VALUES] = None


class Pick(_Strict):
    select: Selector
    count: Annotated[int, Field(ge=1)] = None
    prefer: Prefer = None
    ties: Ties = None


class AnswerPick(Pick):
    """`Answer.pick`. FD2: 0 is a legal count here - "choose none" - alongside
    the decision's own default (left out) and any positive count. Every other
    Pick (costs, blocks, crew) still names at least one object."""

    count: Annotated[int, Field(ge=0)] = None


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


def _x_tag(value: Any) -> str:
    return "x-count" if isinstance(value, int) and not isinstance(value, bool) else "x-policy"


#: `CastMove.x`/`ActivateMove.x` (FD2, CR 107.3): an integer 0-30, or `max`
#: for the largest X the payer's mana can afford right now - the same
#: count-or-policy shape `Amount` already gives a pay-amount decision.
XValue = Annotated[
    Annotated[Annotated[int, Field(ge=0, le=MAX_X)], Tag("x-count")]
    | Annotated[Literal["max"], Tag("x-policy")],
    Discriminator(_x_tag),
]


class ModeChoice(_Strict):
    """One mode of a `CastMove.modes` list (CR 700.2a): the mode named by an
    ability-text phrase, never a mode's literal printed number - the same
    card-name-free discipline every other phrase field keeps."""

    text_has: NonBlank


class OptionalCost(_Strict):
    """One optional cost of a `CastMove.optional_costs` list (CR 118.8):
    kicker, bargain, collect evidence and the rest of what the api's own
    `CastRequest.optional_costs` accepts, named by an ability-text phrase
    rather than the cost's own keyword, so the vocabulary never has to track
    the engine's own keyword spelling for this field."""

    text_has: NonBlank


#: `CastMove.shares` (FD2, CR 601.2d): how a divided effect (Electrolyze's
#: damage, distributed counters) splits among `targets` - one entry per
#: target, in the same order, the runner's own job to align.
Shares = Annotated[list[AtLeastZero], Field(max_length=MAX_TARGETS)]


class ZonedCard(_Strict):
    zone: Literal[COUNT_ZONES]
    select: Selector
    prefer: Prefer = None
    ties: Ties = None


class CardTarget(_Strict):
    """A card or permanent target: what `select` matches, on `side`. Ranked
    the same way any other selector-driven pick is (FB2 T1): `prefer`/`ties`
    narrow and break ties among the legal candidates, and `zone` narrows
    which zones are searched - left out, a target still resolves over
    battlefield and stack, today's behaviour."""

    select: Selector
    side: Side
    prefer: Prefer = None
    ties: Ties = None
    zone: TargetZone = None


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


def answer_shape_problem(answer: Any) -> str | None:
    """Why this answer item's fields do not fit its decision kind, or None.

    A pay-amount decision (`AMOUNT_DECISION_KINDS`) is answered with `amount`
    and nothing else; `assign_combat_damage` (`DAMAGE_DECISION_KINDS`, FD2)
    the same way with `damage` alone - no other decision kind takes either.
    `choice` (O6) is answered only by `CHOICE_DECISION_KINDS`
    (`choose_a_mode`/`choose_a_quality`); `choice_from` (FD2) only by
    `CHOICE_FROM_DECISION_KINDS` (`choose_a_quality`), and never beside
    `choice` - the two name the same decision two different ways; `top_order`
    (FB2 A1) only by `TOP_ORDER_DECISION_KINDS` (`scry`/`surveil`); `position`
    (FB2 A1) only by `POSITION_DECISION_KINDS` (`put_on_top_or_bottom`), and
    never beside `to_bottom` - the two name the same decision two different
    ways; `targets` (FB2 A2) only by `TARGETS_DECISION_KINDS`
    (`choose_targets`), and never beside `pick` for the same reason; `order`
    (FD2) only by `ORDER_DECISION_KINDS` (`order_triggers`). Everywhere else
    these are `answer-shape-mismatch`, the same footing `amount` and `choice`
    are on. A decision outside `DECISION_KINDS` is judged by
    `unknown-decision-kind` instead."""
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
    if decision in DAMAGE_DECISION_KINDS:
        others = [field for field in NON_DAMAGE_ANSWER_FIELDS if field in answer]
        if others:
            return f"'{decision}' is answered with damage, not {', '.join(others)}"
        if "damage" not in answer:
            return f"'{decision}' is answered with damage; name a policy"
        return None
    if "damage" in answer:
        return f"only {', '.join(DAMAGE_DECISION_KINDS)} take damage, not '{decision}'"
    if "choice" in answer and decision not in CHOICE_DECISION_KINDS:
        return f"only {', '.join(CHOICE_DECISION_KINDS)} take a choice, not '{decision}'"
    if "choice_from" in answer and decision not in CHOICE_FROM_DECISION_KINDS:
        return f"only {', '.join(CHOICE_FROM_DECISION_KINDS)} take choice_from, not '{decision}'"
    if "choice_from" in answer and "choice" in answer:
        return (
            "'choice_from' and 'choice' answer the same decision two different ways; use only one"
        )
    if "top_order" in answer and decision not in TOP_ORDER_DECISION_KINDS:
        return f"only {', '.join(TOP_ORDER_DECISION_KINDS)} take a top_order, not '{decision}'"
    if "position" in answer and decision not in POSITION_DECISION_KINDS:
        return f"only {', '.join(POSITION_DECISION_KINDS)} take a position, not '{decision}'"
    if "position" in answer and "to_bottom" in answer:
        return (
            "'position' and 'to_bottom' answer the same decision two different ways; use only one"
        )
    if "targets" in answer and decision not in TARGETS_DECISION_KINDS:
        return f"only {', '.join(TARGETS_DECISION_KINDS)} take targets, not '{decision}'"
    if "targets" in answer and "pick" in answer:
        return "'targets' and 'pick' answer the same decision two different ways; use only one"
    if "order" in answer and decision not in ORDER_DECISION_KINDS:
        return f"only {', '.join(ORDER_DECISION_KINDS)} take order, not '{decision}'"
    return None


class Answer(_Strict):
    """How to answer one pending decision of kind `decision`. A pay-amount
    decision takes `amount` alone; `choice` (O6) is taken only by
    `CHOICE_DECISION_KINDS`; `top_order`/`position` (FB2 A1) and `targets`
    (FB2 A2) are each taken only by their own decision kinds; every other
    kind takes `pick`, `accept`, `to_graveyard`, `to_bottom`, `pile_one`,
    `to_hand` or `players`."""

    decision: DecisionKind
    pick: AnswerPick = None
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
    #: `put_into_hand_or_graveyard`'s own answer (Dina's Guidance, CR 608.2d)
    #: - `True` for the hand, `False` for the graveyard. Not `accept`, for
    #: the same reason `pile_one` is not.
    to_hand: bool = None
    #: `choose_players`'s own answer (CR 701.34a's own Proliferate player
    #: half) - which side(s) to choose among `pending_decision.players`,
    #: resolved to real usernames by `runtime.decide_body` the same way a
    #: `PlayerTarget`'s `player` field already is. Not a `Selector`: a player
    #: has no characteristics a selector could match against.
    players: list[Literal["me", "opponent"]] = Field(default=None, max_length=MAX_PLAYERS_ANSWER)
    #: A scry/surveil's own answer (FB2 A1): tiers ordering the cards kept on
    #: top, first tier on top - an alternative to leaving everything in
    #: looked-at order.
    top_order: TopOrder = None
    #: `put_on_top_or_bottom`'s own answer (FB2 A1, Subtlety's "its owner
    #: puts it on the top or bottom of their library") - an alternative to
    #: `to_bottom`, never both.
    position: Literal["top", "bottom"] = None
    #: `choose_targets`'s own answer (FB2 A2) - an alternative to `pick` that
    #: can also name a player target, the same shapes a `cast`/`activate`
    #: target already takes. FD2: 0 targets is legal - "choose none".
    targets: list[Target] = Field(default=None, min_length=0, max_length=MAX_TARGETS)
    #: `order_triggers`'s own answer (FD2, CR 603.3b): 1-6 selector tiers
    #: naming the pending triggered abilities in the order they go on the
    #: stack - the FIRST tier here goes on the stack FIRST and so resolves
    #: LAST (CR 603.3b's own "bottom of the stack first"); the LAST tier goes
    #: on last and resolves first, on top.
    order: Order = None
    #: `assign_combat_damage`'s own answer (FD2, CR 510.1c/702.19b): a policy
    #: rather than a literal amount per object - `DamagePolicy` below.
    damage: DamagePolicy = None
    #: `choose_a_quality`'s own alternative answer (FD2): a card resolved at
    #: run time by selector, never a name written in the file - the runtime
    #: derives the actual choice text from whichever card the selector
    #: matches. An alternative to `choice`, never both.
    choice_from: ChoiceFromCard = None

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
        if decision in COMBAT_DECISION_KINDS:
            raise PydanticCustomError(
                "use_combat_move",
                "'{decision}' is answered by the '{decision}' move, not an answer item",
                {"decision": decision},
            )
        if decision in FORBIDDEN_DECISION_KINDS:
            raise PydanticCustomError(
                "forbidden_decision",
                "'{decision}' is not answered by a script; "
                "scripts do not assign combat damage or mulligan",
                {"decision": decision},
            )
        return decision


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
    """Names its ability by exactly one of `ability_text_has` (a phrase) or
    `ability_index` (FB2 M4, the card's own ability list index, 0-15) - both
    or neither is `activate-ability-ref`. FD2 adds `exile` (a Pick, the same
    footing `sacrifice`/`discard` are on for an activation cost's own
    "Exile ..." piece), `x` (CR 107.3a's X, announced as the activation
    begins) and `color` (which coloured symbol a mana ability with more than
    one colour to give covers)."""

    kind: Literal["activate"]
    source: ZonedCard
    ability_text_has: NonBlank = None
    ability_index: AbilityIndex = None
    targets: list[Target] = Field(default_factory=list, max_length=MAX_TARGETS)
    sacrifice: Sacrifice = None
    discard: Discard = None
    exile: Pick = None
    x: XValue = None
    color: Color = None
    answers: list[Answer] = Field(default_factory=list, max_length=MAX_ANSWERS)

    @model_validator(mode="after")
    def _exactly_one_ability_reference(self) -> ActivateMove:
        named = (self.ability_text_has is not None) + (self.ability_index is not None)
        if named != 1:
            raise PydanticCustomError(
                "activate_ability_ref",
                "name exactly one of ability_text_has or ability_index",
            )
        return self


class CastMove(_Strict):
    """FD2 additions, all optional and additive: `face` (a face selector for
    a split/Adventure/MDFC spell face - by type or text phrase, never a card
    name, the same discipline every other selector field keeps), `modes`
    (CR 700.2a, 1-5 modes each named by a phrase), `x` (CR 107.3a),
    `shares` (CR 601.2d, aligned with `targets` by position), `optional_costs`
    (CR 118.8, 1-4 of them), the cost-object Picks `sacrifice`/`discard`/
    `exile`/`tap` for a printed additional cost naming one of those, and the
    `convoke`/`delve`/`improvise` Picks (CR 702.51a/702.66a/702.126a)."""

    kind: Literal["cast"]
    card: ZonedCard
    cost: CastCost = None
    face: Selector = None
    modes: Annotated[list[ModeChoice], Field(min_length=1, max_length=MAX_MODES)] = None
    x: XValue = None
    shares: Shares = None
    optional_costs: Annotated[
        list[OptionalCost], Field(min_length=1, max_length=MAX_OPTIONAL_COSTS)
    ] = None
    sacrifice: Pick = None
    discard: Pick = None
    exile: Pick = None
    tap: Pick = None
    convoke: Pick = None
    delve: Pick = None
    improvise: Pick = None
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


class PassMove(_Strict):
    """Release priority once (CR 117.3d, FB2 M1) - no `answers` field, since
    there is nothing to answer. Must be the play's last move
    (`Play._pass_last_and_combat_first` below), otherwise `pass-not-last`. A
    play whose only move is `pass` is a legal "do nothing here" play."""

    kind: Literal["pass"]


class DefenderPlayer(_Strict):
    player: Literal["opponent"]


class DefenderCard(_Strict):
    """A planeswalker or battle the attack group is declared against."""

    select: Selector
    side: Literal["opponent"]


def _defender_tag(value: Any) -> str:
    return "defender-player" if isinstance(value, dict) and "player" in value else "defender-card"


#: An `AttackGroup`'s own defender (FB2 M2): the opponent player (the
#: default, left out), or a permanent of theirs matched by selector.
Defender = Annotated[
    Annotated[DefenderPlayer, Tag("defender-player")]
    | Annotated[DefenderCard, Tag("defender-card")],
    Discriminator(_defender_tag),
]


def _attack_count_tag(value: Any) -> str:
    return (
        "attack-count-number"
        if isinstance(value, int) and not isinstance(value, bool)
        else "attack-count-all"
    )


#: How many of the matching creatures attack: a count from 1, or every one
#: of them.
AttackCount = Annotated[
    Annotated[Annotated[int, Field(ge=1)], Tag("attack-count-number")]
    | Annotated[Literal["all"], Tag("attack-count-all")],
    Discriminator(_attack_count_tag),
]


class AttackGroup(_Strict):
    """One group of the viewer's untapped creatures matching `select`,
    `count` of them (or `all`), best first by `prefer`, declared against
    `defender` (left out: the opponent player)."""

    select: Selector
    count: AttackCount = None
    prefer: Prefer = None
    ties: Ties = None
    defender: Defender = None


class DeclareAttackersMove(_Strict):
    """CR 508.1 (FB2 M2). `attackers: []` declares no attackers - still a
    legal declare-attackers move. Must be the play's first move
    (`Play._pass_last_and_combat_first` below), otherwise
    `combat-move-not-first`."""

    kind: Literal["declare_attackers"]
    attackers: list[AttackGroup] = Field(default_factory=list, max_length=MAX_ATTACK_GROUPS)


class BlockerPick(Pick):
    """A block's own `blocker` half: the viewer's untapped creatures matching
    `select`, `count` of them (default 1), best first by `prefer`."""


class AttackerPick(_Strict):
    """A block's own `attacker` half: which attacking creature this block
    answers, by selector."""

    select: Selector
    prefer: Prefer = None
    ties: Ties = None


class Block(_Strict):
    blocker: BlockerPick
    attacker: AttackerPick


class DeclareBlockersMove(_Strict):
    """CR 509.1 (FB2 M3). `blocks: []` declares no blocks - still a legal
    declare-blockers move. Must be the play's first move
    (`Play._pass_last_and_combat_first` below), otherwise
    `combat-move-not-first`."""

    kind: Literal["declare_blockers"]
    blocks: list[Block] = Field(default_factory=list, max_length=MAX_BLOCKS)


class KeywordActionMove(_Strict):
    """FD2: one shape for the six existing keyword-action routes (suspend,
    plot, foretell, cycle, equip, crew), FD5's two new ones (turn_face_up,
    companion), and CR 709.5e's `unlock_door` - `action` names which, `card`
    locates the card the way a `cast`/`play_land` card already does, `target`
    names the creature an `equip` attaches to (a `CardTarget`, never a
    player), `crew_with` names the creatures a `crew` action taps to pay its
    cost, `station_with` the one creature a `station` action (CR 702.184a)
    taps, and `door` names the locked half `unlock_door` pays for (the
    half's own printed name) - meaningful only for that one action.
    `level_up` (CR 702.87a) names only its leveler; `reconfigure` (CR
    702.151a) attaches its Equipment to the `target` creature, or, with no
    `target`, is the unattach ability instead."""

    kind: Literal["keyword_action"]
    action: KeywordAction
    card: ZonedCard
    target: CardTarget = None
    crew_with: Pick = None
    station_with: Pick = None
    door: str | None = None
    answers: list[Answer] = Field(default_factory=list, max_length=MAX_ANSWERS)


class MulliganMove(_Strict):
    """FD2: CR 103.5's declaration - `keep` false takes a mulligan, true
    keeps the hand dealt (drawing a fresh seven either way is London
    mulligan's own rule, not this move's business); a seat that has already
    mulliganed and keeps also owes CR 103.5's bottoming, named by `bottom`
    (left out, the engine's own default - the back of the hand). Valid only
    when `when.steps` is exactly `["mulligan"]`
    (`Play._mulligan_move_needs_the_mulligan_step` below), otherwise
    `mulligan-wrong-step` - the declaration this move makes is meaningless
    anywhere else."""

    kind: Literal["mulligan"]
    keep: bool
    bottom: Pick = None


class RepeatMove(_Strict):
    """FD9: repeat `moves` (1-`MAX_REPEAT_BODY` of the existing move kinds,
    none of them `repeat` itself - `repeat-nested` - or `pass` -
    `repeat-pass-in-body`: a loop's body commits real moves and exits through
    `until`, `max_iterations` or a refusal, never by releasing priority)
    until every one of `until`'s 1-`MAX_CONDITIONS` conditions holds -
    `repeat-empty-until` when the list is empty. `until` is judged fresh
    against a freshly read board before every iteration, so an `until` that
    already holds runs the loop zero times. `max_iterations` is required,
    1-`MAX_REPEAT_ITERATIONS` (missing or out of range is `repeat-bound`):
    the loop's own hard stop, distinct from whatever `until` alone would
    leave unbounded. `stop_when_refused` (default false) turns an engine
    refusal mid-loop into a normal end of the loop instead of a run failure.
    Counts once toward `MAX_MOVES`/`MAX_MOVES_PER_RUN`; its own iterations
    are bounded only by `max_iterations`. The runtime that actually drives
    the loop (each iteration's board re-read, the no-progress guard, the
    resume token) is FD3 part 7's, in `playscripts.runtime` - see the spec's
    own 'Repeat' section for its exact stop rules."""

    kind: Literal["repeat"]
    moves: list[Move] = Field(min_length=1, max_length=MAX_REPEAT_BODY)
    until: list[Condition] = Field(max_length=MAX_CONDITIONS)
    max_iterations: int = None
    stop_when_refused: bool = False

    @model_validator(mode="after")
    def _body_excludes_repeat_and_pass(self) -> RepeatMove:
        for move in self.moves:
            if isinstance(move, RepeatMove):
                raise PydanticCustomError(
                    "repeat_nested",
                    "a repeat's own moves may not contain another repeat; nesting is refused",
                )
            if isinstance(move, PassMove):
                raise PydanticCustomError(
                    "repeat_pass_in_body",
                    "a repeat's own moves may not contain pass; the loop exits through "
                    "until, max_iterations or a refusal, never by releasing priority",
                )
        return self

    @model_validator(mode="after")
    def _until_is_not_empty(self) -> RepeatMove:
        if not self.until:
            raise PydanticCustomError(
                "repeat_empty_until",
                "until names at least one condition; an empty until can never stop the loop",
            )
        return self

    @model_validator(mode="after")
    def _max_iterations_is_bounded(self) -> RepeatMove:
        value = self.max_iterations
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or not (1 <= value <= MAX_REPEAT_ITERATIONS)
        ):
            raise PydanticCustomError(
                "repeat_bound",
                "max_iterations is required, an integer from 1 to {max}",
                {"max": MAX_REPEAT_ITERATIONS},
            )
        return self


Move = Annotated[
    ActivateMove
    | CastMove
    | PlayLandMove
    | AnswerMove
    | PassMove
    | DeclareAttackersMove
    | DeclareBlockersMove
    | KeywordActionMove
    | MulliganMove
    | RepeatMove,
    Field(discriminator="kind"),
]
RepeatMove.model_rebuild()


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

    @field_validator("true")
    @classmethod
    def _all_of_only_in_false(cls, conditions: list[Any]) -> list[Any]:
        if any(isinstance(condition, AllOfCondition) for condition in conditions):
            raise PydanticCustomError(
                "all_of_outside_false",
                "'all_of' means not (A and B ...); it is allowed only in false, not true",
            )
        return conditions

    @model_validator(mode="after")
    def _pass_last_and_combat_first(self) -> Play:
        """A combat move (`declare_attackers`/`declare_blockers`) must be the
        *play's* first move, and a play carries at most one. A repeat's own
        body-index-0 does not by itself satisfy that - the repeat move
        carrying it must itself be the play's move 0, and no second combat
        move may sit anywhere else in the play, top-level or nested."""
        last_index = len(self.moves) - 1
        combat_moves: list[tuple[bool, DeclareAttackersMove | DeclareBlockersMove]] = []
        for index, move in enumerate(self.moves):
            if isinstance(move, PassMove) and index != last_index:
                raise PydanticCustomError("pass_not_last", "'pass' must be the last move of moves")
            if isinstance(move, DeclareAttackersMove | DeclareBlockersMove):
                combat_moves.append((index == 0, move))
            if isinstance(move, RepeatMove):
                for body_index, body_move in enumerate(move.moves):
                    if isinstance(body_move, DeclareAttackersMove | DeclareBlockersMove):
                        combat_moves.append((index == 0 and body_index == 0, body_move))
        if combat_moves and (len(combat_moves) > 1 or not combat_moves[0][0]):
            _, offending = next(
                ((is_first, move) for is_first, move in combat_moves if not is_first),
                combat_moves[0],
            )
            raise PydanticCustomError(
                "combat_move_not_first",
                "'{kind}' must be the first move of moves",
                {"kind": offending.kind},
            )
        return self

    @model_validator(mode="after")
    def _standalone_pending_answer_names_the_pending_decision(self) -> Play:
        if len(self.moves) == 1 and isinstance(self.moves[0], AnswerMove):
            pending = {
                condition.decision
                for condition in self.true
                if isinstance(condition, PendingDecisionCondition)
            }
            if pending:
                for answer in self.moves[0].answers:
                    if answer.decision not in pending:
                        raise PydanticCustomError(
                            "answer_decision_mismatch",
                            "this play's pending_decision condition names "
                            "{pending}, but the answer names '{decision}'",
                            {"pending": ", ".join(sorted(pending)), "decision": answer.decision},
                        )
        return self

    @model_validator(mode="after")
    def _mulligan_move_needs_the_mulligan_step(self) -> Play:
        """FD2: a `mulligan` move's declaration is meaningless outside CR
        103.5's own step - `when.steps` must name it and nothing else,
        otherwise `mulligan-wrong-step`. A repeat's own body is walked too -
        nesting is the only thing `repeat` refuses, not the play-level rules
        that apply to every move it carries."""
        has_mulligan = any(
            isinstance(move, MulliganMove)
            or (
                isinstance(move, RepeatMove)
                and any(isinstance(body_move, MulliganMove) for body_move in move.moves)
            )
            for move in self.moves
        )
        if has_mulligan and list(self.when.steps) != ["mulligan"]:
            raise PydanticCustomError(
                "mulligan_wrong_step",
                "a 'mulligan' move is valid only with when.steps == ['mulligan'], not {steps}",
                {"steps": list(self.when.steps)},
            )
        return self


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
    "until": TypeAdapter(Condition),
    "conditions": TypeAdapter(LeafCondition),
    "answers": TypeAdapter(Answer),
    "next": TypeAdapter(NonBlank),
    "steps": TypeAdapter(StepName),
    "targets": TypeAdapter(Target),
    "prefer": TypeAdapter(Selector),
    "top_order": TypeAdapter(Selector),
    "attackers": TypeAdapter(AttackGroup),
    "blocks": TypeAdapter(Block),
    "opposing_archetypes": TypeAdapter(ArchetypeKey),
    "zones": TypeAdapter(Literal[MISSING_FROM_ZONES]),
    "searches_for_any": TypeAdapter(Literal[BASIC_LAND_TYPES]),
    "enters_any": TypeAdapter(Literal[ENTERS_STATES]),
    "payable_any": TypeAdapter(Literal[PAYABLE_STATES]),
    "modes": TypeAdapter(ModeChoice),
    "optional_costs": TypeAdapter(OptionalCost),
    "order": TypeAdapter(Selector),
    "tiers": TypeAdapter(Selector),
    "shares": TypeAdapter(AtLeastZero),
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
    "pass_not_last": "pass-not-last",
    "combat_move_not_first": "combat-move-not-first",
    "activate_ability_ref": "activate-ability-ref",
    "all_of_outside_false": "all-of-outside-false",
    "use_combat_move": "use-combat-move",
    "answer_decision_mismatch": "answer-decision-mismatch",
    "mulligan_wrong_step": "mulligan-wrong-step",
    "repeat_nested": "repeat-nested",
    "repeat_pass_in_body": "repeat-pass-in-body",
    "repeat_empty_until": "repeat-empty-until",
    "repeat_bound": "repeat-bound",
}
#: A value outside a closed Literal, by the field it is in.
_LITERAL_CODES = {"decision": "unknown-decision-kind", "cost": "unknown-cast-cost"}
#: The tags of the unions discriminated by shape rather than by `kind`. They
#: hold a hyphen, so no key of a document is ever spelled like one.
_SHAPE_TAGS = frozenset(
    {"amount-count", "amount-policy", "card-target", "player-target"}
    | {"sacrifice-keyword", "sacrifice-choice"}
    | {"discard-keyword", "discard-choice"}
    | {"defender-player", "defender-card"}
    | {"attack-count-number", "attack-count-all"}
    | {"x-count", "x-policy"}
)
_MISSING = object()


def _walk(doc: Any, loc: tuple[Any, ...]) -> tuple[str, Any]:
    """The dotted location of `loc` in `doc`, and the value found there.

    Pydantic puts a discriminated union's tag into the location: after the
    list index for a `kind`/`of` union, after the field for a shape union
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
        tag = None
        if isinstance(value, dict):
            tag = value.get("kind")
            if tag is None:
                tag = value.get("of")
        return "forbidden-kind" if isinstance(tag, str) else "malformed-play"
    return "malformed-play"


def _message(error: dict[str, Any], code: str) -> str:
    if code == "forbidden-kind" and error["type"] == "union_tag_invalid":
        value = error.get("input")
        tag = value.get("kind") or value.get("of") if isinstance(value, dict) else None
        return f"kind {tag!r} is not in the closed vocabulary"
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
