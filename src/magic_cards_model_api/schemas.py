"""Request bodies. Deliberately loose: this is a self-test harness."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class NewPlayer(BaseModel):
    username: str = Field(min_length=1)
    life: int = 20


class NewGame(BaseModel):
    players: list[NewPlayer] = Field(min_length=1)
    active_player: str | None = None
    """Defaults to the first player listed."""
    step: str = "precombat_main"
    """Step the game starts in. Any Step value."""


class PutCard(BaseModel):
    card: str
    """A card name. Always fetched from Scryfall (cache first)."""
    player: str
    zone: str = "hand"
    position: str | None = None
    """For the library: "top" (default) or "bottom"."""
    tapped: bool = False
    face_up: bool = True


class MoveCard(BaseModel):
    to_zone: str
    player: str | None = None
    """The player whose zone receives the card (their hand, library, ...)."""
    controller: str | None = None
    """New controller, for shared zones (battlefield, exile)."""
    position: str | None = None
    tapped: bool | None = None
    face_up: bool | None = None


class PatchCard(BaseModel):
    tapped: bool | None = None
    face_up: bool | None = None
    damage: int | None = None
    summoning_sick: bool | None = None
    counters: dict[str, int] | None = None
    """Counter kind (e.g. "+1/+1", "loyalty") -> count to set."""


class PatchPlayer(BaseModel):
    life: int | None = None
    counters: dict[str, int] | None = None
    """Counter kind (e.g. "poison", "energy") -> count to set."""


class ManaPoolUpdate(BaseModel):
    empty: bool = False
    add: dict[str, int] | None = None
    """Color letter ("W", "U", "B", "R", "G", "C") -> amount."""
    set: dict[str, int] | None = None
    """Replace the whole pool with this."""


class StepUpdate(BaseModel):
    step: str
    player: str | None = None
    """Who is moving the marker, for the log's who-did-what line."""


class ActorRequest(BaseModel):
    """An optional body for endpoints that take no other input: just who is
    calling, so the audit log can attribute the action."""

    player: str | None = None


class ReportProblemRequest(BaseModel):
    player: str
    text: str = Field(min_length=1)
    """What the player thinks is wrong, written by the player."""


class ManaSource(BaseModel):
    card_id: UUID
    colors: list[str] | None = None
    """Which colors to tap it for; defaults to the ability's own production."""


class CastRequest(BaseModel):
    player: str
    card_id: UUID | None = None
    card: str | None = None
    """Card name, used when no card_id is given: first match in from_zone."""
    from_zone: str = "hand"
    targets: list[str] | None = None
    """Card ids or player names."""
    modes: list[int] | None = None
    x_value: int | None = None
    shares: list[int] | None = None
    """CR 601.2d: how a divided effect (Electrolyze's damage, distributed
    counters) is split among `targets`, one entry per target in the same
    order. Only for a card whose affordance carries `division`."""
    mana_sources: list[UUID | ManaSource] | None = None
    keyword: str | None = None
    """The keyword permission this cast is announced under (CR 601.2b) -
    required for a cast whose card is not in `player`'s hand, since CR 601.3
    ("a player can begin to cast a spell only if a rule or effect allows
    that player to cast it") makes casting from anywhere else illegal unless
    *some* named permission says otherwise, and this is how that permission
    is named. Every one of these is read straight off the engine's own
    `machines/casting.py::keyword_cast_options`/`marker_cast_options`/
    `granted_cast_options` - see `serialize.py`'s `cast_options` field on a
    graveyard/exile card view (and `available_actions`'s `castable` entries)
    for which keyword a given card actually offers right now, rather than
    guessing from this list:

    - `"Flashback"` - a card in the GRAVEYARD with flashback (CR 702.34a).
    - `"Mayhem"` - a card in the GRAVEYARD with mayhem, discarded this turn
      (CR 702.187a/b).
    - `"Plot"` - a card in EXILE, plotted on an earlier turn (CR 702.170d).
    - `"Foretell"` - a card in EXILE, foretold on an earlier turn
      (CR 702.143a).
    - `"Madness"` - a card in EXILE via its madness trigger, same turn
      (CR 702.35a/b).
    - `"Airbend"` - a card in EXILE via an airbend effect, for {2}
      (CR 701.65a).
    - a printed CR 604.6/CR 113.6f sentence ("You may cast this card from
      your graveyard using its X ability") opening one of the above keywords
      in a zone it does not otherwise reach - `keyword` is still that
      keyword's own name, only the zone changed.
    - any keyword another permanent's static ability is granting this spell
      right now (CR 610.5's "... spells you cast ... gain X as you cast
      them", or CR 611.3d's "each ... card in your graveyard has X") - named
      the same way a printed one would be.

    A card freshly granted a `PlayPermission` (suspend's own free cast, CR
    702.62a's third ability; cascade; Past in Flames) needs no `keyword`:
    that permission is found and used automatically whenever one is live for
    this player and card - see `play_permissions` on the state view."""
    convoke: list[ConvokeChoice] | None = None
    """Creatures tapped for convoke (CR 702.51a). Applied after the total
    cost is determined, never as part of the announcement."""
    delve: list[UUID] | None = None
    """Cards exiled from the payer's graveyard for delve (CR 702.66a), one
    per generic mana in the total cost."""
    alternative_cost: int | str | None = None
    """The CR 118.9 alternative cost this spell is announced under, as either
    its `index` in `GET .../affordances/{card_id}`'s `alternative_costs` list
    or the option's keyword/source name. This is how Force of Negation gets
    pitched: a seat had no way to *find* an alternative cost before, and so
    could not announce one - see `affordances.alternative_costs`. Announced
    at CR 601.2b, ahead of targets, and mutually exclusive with `keyword`
    (which is the same announcement for the specific keyword origin).
    Only one alternative cost may ever be paid (CR 118.9a)."""
    choosing: list[UUID] | None = None
    """The objects an alternative cost requires the caster to supply - the
    cards pitched to Force of Negation, the Mountains sacrificed to Fireblast.
    `alternative_costs[i].requires` says how many are needed."""
    dry_run: bool = False


class ConvokeChoice(BaseModel):
    """One creature tapped to pay one symbol of a convoke spell's cost.

    CR 702.51b: convoke "isn't an additional or alternative cost and applies
    only after the total cost of the spell with convoke is determined", so
    this is not part of the announcement - it is which objects the payer
    offers against the already-determined cost."""

    card_id: UUID
    color: str | None = None
    """The coloured symbol this creature covers (CR 702.51a: only a colour
    the creature actually is). Omitted, it is offered against a generic
    symbol instead."""


class CrewRequest(BaseModel):
    """CR 702.122a: "Crew N" means "Tap any number of other untapped
    creatures you control with total power N or greater: This permanent
    becomes an artifact creature until end of turn."

    Crew is an activated ability of the Vehicle, not of the creatures, and it
    has no `{T}` symbol of its own - so this is not an `ActivateRequest`
    against an ability index. The creatures are the cost."""

    player: str
    card_id: UUID
    """The Vehicle being crewed."""
    crewing: list[UUID] = Field(min_length=1)
    """The creatures tapped to pay for it (CR 702.122b: "a creature crews a
    Vehicle when it's tapped to pay the cost")."""
    dry_run: bool = False


class CycleRequest(BaseModel):
    """CR 702.29a: "[Cost], Discard this card: Draw a card." An activated
    ability, but one `Game.begin_activating`/`activate_ability` cannot reach -
    they only ever activate a battlefield permanent's ability, and cycling
    "functions only while the card... is in a player's hand" (CR 702.29b) - so
    it gets its own request shape and endpoint rather than reusing
    `ActivateRequest`."""

    player: str
    card_id: UUID | None = None
    card: str | None = None
    """Card name, used when no card_id is given: first match in the player's hand."""
    ability_index: int = 0
    """Which of the card's cycling abilities (printed order) - almost always
    0; only a card with more than one line ending "cycling" needs another."""
    mana_sources: list[UUID | ManaSource] | None = None
    dry_run: bool = False


class SpecialActionRequest(BaseModel):
    """A CR 116.2 special action that exiles a card from hand to cast later:
    suspend (CR 116.2f), plot (CR 116.2k), or foretell (CR 116.2h). All three
    share this shape - locate the card, pay its special-action cost, exile it
    into its own marked pile - so one request body serves all three actions."""

    player: str
    card_id: UUID | None = None
    card: str | None = None
    """Card name, used when no card_id is given: first match in the player's hand."""
    mana_sources: list[UUID | ManaSource] | None = None
    dry_run: bool = False


class TargetRequest(BaseModel):
    source_id: UUID
    """A spell on the stack, a card in hand, or a permanent."""
    ability_index: int | None = None
    """For an activated ability on a permanent."""
    mode_index: int | None = None
    target: str
    """A card id or a player name."""


class ActivateRequest(BaseModel):
    player: str
    card_id: UUID
    ability_index: int = 0
    targets: list[str] | None = None
    mana_sources: list[UUID | ManaSource] | None = None
    sacrificing: UUID | None = None
    discarding: UUID | None = None
    color: str | None = None
    """Color letter ("W", "U", "B", "R", "G", "C") chosen for a mana ability
    that offers one - an "any color" source or a dual land. Ignored by any
    ability that does not need a choice."""
    dry_run: bool = False


class AttackPair(BaseModel):
    card_id: UUID
    defender: str
    """A player name or a planeswalker card id."""


class AttackRequest(BaseModel):
    attackers: list[AttackPair] = Field(min_length=1)
    dry_run: bool = False


class PlayLandRequest(BaseModel):
    player: str
    card_id: UUID | None = None
    card: str | None = None
    dry_run: bool = False


class PassPriorityRequest(BaseModel):
    player: str


class ConcedeRequest(BaseModel):
    """CR 104.3a: a player may concede at any time, and leaves the game
    immediately. No priority is required and nothing can refuse it, so unlike
    every other action request this one has no `dry_run` - there is nothing to
    ask permission for."""

    player: str


class AdvanceRequest(BaseModel):
    """Who is advancing the turn - required, unlike `ActorRequest`, because
    `Game.advance` now authorizes the call against the active player (or
    their controller, CR 723.5) rather than merely logging who asked."""

    player: str


class BlockPair(BaseModel):
    blocker: UUID
    """The creature declaring the block."""
    blocked: UUID
    """The attacking creature it blocks."""


class DeclareBlockersRequest(BaseModel):
    player: str
    blocks: list[BlockPair] = Field(min_length=1)
    """(blocker, blocked) pairs, in the order damage should be assigned through them."""


class DecideRequest(BaseModel):
    player: str
    """Must be the pending decision's own player; anyone else is rejected."""
    card_ids: list[str] = Field(default_factory=list)
    """The chosen cards, for the `choose_cards` kind (the cleanup discard)."""
    targets: list[str] = Field(default_factory=list)
    """The chosen targets, for the `choose_targets` kind - a triggered
    ability's targets (CR 603.3d), answered here rather than through
    `actions/cast` or `actions/activate` because a trigger is put on the stack
    by the engine itself, with no announcement request of its own to carry
    them. Each entry is a card or ability id, or a player's username - the
    same token shape `CastRequest.targets` and `ActivateRequest.targets`
    already use, so a client does not learn a second format for "a target"."""
    accept: bool | None = None
    """The yes/no answer, for the `may`, `pay_life`, and `pay_energy` kinds
    (CR 608.2d, CR 119.4, CR 118.3) - a shockland's "pay 2 life" is a
    `pay_life` decision, a fixed "pay {E}{E}" is `pay_energy`. `None` (the
    default) is refused rather than treated as "no": a client must say which
    one it means."""
    energy_amount: int | None = None
    """The `pay_energy_amount` kind's answer (CR 107.14, CR 118.3) - "pay any
    amount of {E}" (Galvanic Discharge), an integer from 0 up to
    `pending_decision.amount` (the energy the player actually has). Not the
    same question as `pay_energy`'s `accept`: this kind asks how much, not
    whether. `None` (the default) is refused, matching `accept`."""
    choice: str | None = None
    """The `choose_a_quality` kind's answer (CR 614.12a) - "as this enters,
    choose a color/card name/creature type". A color choice is narrowed to
    `pending_decision.options`; a card name or creature type choice offers no
    `options` (the universe is too large to enumerate) and accepts any
    non-empty string. `None` (the default) is refused, matching `accept`.
    Also the `choose_a_mode` kind's answer (CR 603.3c, CR 700.2a) - a modal
    triggered ability's "choose one -" (Elder Gargaroth's attack/block
    trigger): one of `pending_decision.options`, the ability's own printed
    modes, always non-empty for this kind unlike `choose_a_quality`'s open
    shapes. Shares this field rather than growing its own - both kinds are
    "which of these printed strings did you pick"."""
    life_amount: int | None = None
    """The `pay_life_amount` kind's answer (CR 119.4) - "you may pay any
    amount of life" (Necrodominance), an integer from 0 up to
    `pending_decision.amount` (the player's life total). Not the same
    question as `pay_life`'s `accept`: this kind asks how much, not whether.
    Distinct from `energy_amount` - the two pay from different resources.
    `None` (the default) is refused, matching `accept`/`energy_amount`."""
    moved: list[str] = Field(default_factory=list)
    """For the `scry` and `surveil` kinds: which of `pending_decision.cards`
    move off the top - to the bottom of the library for a scry (CR 701.22a),
    to the graveyard for a surveil (CR 701.25a). Every card not named here
    goes back on top. Empty is a real answer: "keep everything on top"."""
    top_order: list[str] | None = None
    """For `scry`/`surveil`: the order the cards that stay on top go back,
    topmost first. `None` (the default) keeps them in the order they were
    looked at, which is what a rigged library needs to stay rigged."""
    top: bool | None = None
    """The `put_on_top_or_bottom` kind's answer (Subtlety, CR 608.2d) - `True`
    for the top of the library, `False` for the bottom. Asked of the targeted
    spell's *owner* (CR 108.3), not of Subtlety's controller - see
    `pending_decision.player`. Not `accept`: this is a top/bottom choice, not
    a yes/no, even though both happen to be booleans. `None` (the default) is
    refused, matching `accept`/`choice`/`energy_amount`."""
    order: list[str] = Field(default_factory=list)
    """The `order_triggers` kind's answer (CR 603.3b) - the order this
    player's simultaneously triggered abilities go on the stack, "in any order
    they choose".

    A permutation of exactly `pending_decision.cards`, which for this kind
    holds `AbilityOnStack` ids rather than card ids - hence its own field
    rather than `card_ids`. **Bottom of the stack first**, so the last id
    listed is the ability that resolves first. Anything that is not a
    permutation (a dropped ability, a duplicate, a stray id) is refused."""


class MulliganRequest(BaseModel):
    """One seat's CR 103.5 declaration: "a player who is dissatisfied with
    their initial hand may take a mulligan"."""

    player: str
    take: bool
    bottom: list[UUID] | None = None
    """Which cards to put on the bottom of the library when keeping.

    CR 103.5 charges one card per counted mulligan, and *which* cards is the
    keeping player's own choice. Required in length exactly when the seat owes
    one (`pregame.cards_to_bottom`); `null` takes the engine's own default -
    the back of the hand - and the audit log records that it was defaulted, not
    chosen. Ignored when `take` is true, since a mulliganed hand is shuffled
    back rather than paid for.
    """


class OpeningHandActionRequest(BaseModel):
    """One CR 103.6 action with a card in this seat's own opening hand -
    "some cards allow a player to take actions with them from their opening
    hand"."""

    player: str
    card_id: UUID
