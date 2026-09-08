"""Request bodies, and (T1) the response views built on top of
`serialize.state_view`/`visibility.redact_view`. Requests are deliberately
loose: this is a self-test harness. Responses are not loose - `/openapi.json`
is a contract three other repos will pin against by sha, so every one of
these must be a named component FastAPI can put in `#/components/schemas`,
not a bare `dict[str, Any]` that serialises to `{"type": "object"}`."""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import UUID

from magic_cards_model.models import Action, Card
from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class NewPlayer(BaseModel):
    username: str = Field(min_length=1)
    life: int = 20
    is_agent: bool = False
    """T10: this seat's kind, recorded once at game creation - `POST
    /api/games` registers every `True` username the same way
    `decks_api.start_game_from_decklists` already registers a decklist's own
    `ai` seats (`store.register_ai_players`), so `store.ai_players(game_id)`
    is the single place either boot path's "is this an agent" fact lands.
    `records.require_reasoning` reads it from there to decide whether a
    mutating action needs `reasoning`."""


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
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one. This route reads like board setup and is not
    one: it moves the turn marker and drives the suspend upkeep the route
    itself runs when it lands on `upkeep`, and every agent has it as an
    MCP tool
    (`mcp.set_step`). An agent moving the game's own turn marker with no
    stated why is exactly what this gate exists to prevent."""


class ActorRequest(BaseModel):
    """An optional body for endpoints that take no other input: just who is
    calling, so the audit log can attribute the action."""

    player: str | None = None
    reasoning: str | None = None
    """T10: this action's own reasoning - required for an agent seat
    (`records.require_reasoning`), optional for a human one."""


#: A field a seat has to actually say something in. Whitespace is stripped
#: first and then required to be non-empty, so `"   "` is refused the same
#: way `""` is - `records.require_reasoning` already treats a
#: whitespace-only reason as none given, and a declaration made of spaces
#: would satisfy the plan gate while telling a reviewer nothing.
Stated = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class OpponentPrediction(BaseModel):
    """What a seat expects its opponent to do next, and why.

    The prediction is the part of a declaration that makes it evidence
    rather than commentary: it is checkable against what the opponent then
    actually did, and `review.py` puts the two side by side without grading
    either. `because` is required for the same reason - a call with no read
    behind it is a guess, and a guess that happens to land proves nothing.
    """

    expecting: Stated
    """What the opponent will do, concretely enough to be wrong about."""
    because: Stated
    """The read that supports it - what on the board, in the log, or in the
    matchup makes this the expected line."""
    confidence: Literal["low", "medium", "high"] = "medium"
    """How strongly. Three levels rather than a percentage: a number invites
    false precision nobody can calibrate, and a sceptic reading the review
    will argue with a 65% far harder than with a "medium"."""


class DeclarePlanIn(BaseModel):
    """`POST /api/games/{game_id}/plan` - a seat's declaration, made before
    it acts, of how it reads this turn.

    Deliberately five fields and no more. Each one is something the post-game
    review has to show and cannot reconstruct from the board: what the seat
    saw (`board_read`), where it thought it stood in the matchup
    (`my_role`), what it meant to do (`plan`), and what it expected back
    (`prediction`, with its own reason and confidence). Anything longer and
    an agent fills it in with filler; anything shorter and the review is
    narrative again rather than evidence.

    `uid` is the seat's own credential, carried in the body beside `player`
    rather than in the query string: a plan is hidden information about how
    a seat intends to play, so the server checks that the caller *is* the
    seat it is declaring for before writing anything into its name.
    """

    player: Stated
    uid: Stated
    """This seat's own uid - `player` must be the seat it opens."""
    board_read: Stated
    """What you see on the board right now - the state you are planning
    against, in your own words."""
    my_role: Stated
    """Your own role in this matchup as you understand it - who is the
    beatdown, what your deck is trying to do, what losing looks like."""
    plan: Stated
    """What you intend to do this turn, and why that follows from the two
    above."""
    prediction: OpponentPrediction
    """What you expect the opponent to do in response, and why."""


class DeclarePlanResponse(BaseModel):
    """`POST .../plan`'s answer. No state view: a declaration mutates no
    `Game`, so there is nothing about the board that this could have
    changed."""

    declared: bool
    log_seq: int
    """The `records` row this declaration became - the same monotonic marker
    every action response carries."""
    player: str
    turn_number: int | None = None
    """The turn this plan is for, as the ledger stamped it."""


class ReflectionIn(BaseModel):
    """`POST /api/games/{game_id}/reflection` - a seat's own post-game
    write-up, filed once the result is already known.

    Deliberately the only shape this ever takes: one seat, one block of
    free text. Unlike `DeclarePlanIn`'s five structured fields, there is
    nothing here a post-game review has to show and cannot reconstruct from
    the board - the whole point of a reflection is that it is the agent's
    own words, in its own shape, written after the game rather than during
    it. Splitting it into board-read/plan/prediction fields the way a
    declaration is split would dress up a narrative as evidence it never
    was; `review.py`'s reflection section already says in words that
    nothing here is evidence of anything the review pairs above it.

    `uid` mirrors `DeclarePlanIn.uid`: the seat's own credential, carried in
    the body, so nobody may write a reflection into another seat's name.
    """

    player: Stated
    uid: Stated
    """This seat's own uid - `player` must be the seat it opens."""
    text: Stated
    """The write-up itself, in the seat's own words."""


class ReflectionResponse(BaseModel):
    """`POST .../reflection`'s answer. No state view, for the same reason
    `DeclarePlanResponse` has none: a reflection mutates no `Game`."""

    recorded: bool
    log_seq: int
    """The `records` row this reflection became."""
    player: str


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
    graveyard/exile card view (and `available_actions`'s `cast`-kind entries,
    each naming its own `keyword`) for which keyword a given card actually
    offers right now, rather than guessing from this list:

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
    reasoning: str | None = None
    """T10: this cast's own reasoning - required for an agent seat
    (`records.require_reasoning`), optional for a human one."""


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
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


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
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


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
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


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
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


class AttackPair(BaseModel):
    card_id: UUID
    defender: str
    """A player name or a planeswalker card id."""


class AttackRequest(BaseModel):
    attackers: list[AttackPair] = Field(default_factory=list)
    """Empty is a legal answer, not a missing one: CR 508.1a's "if any" makes
    "declare nobody" a real attack declaration, the one an autopilot run
    sends (`seatkit.pass_until`) when it has decided not to stop for combat.
    A `min_length=1` here used to make that answer inexpressible through this
    endpoint at all, forcing an empty declaration through `decide()`'s own
    `pairs` field instead - now both reach `decisions.declared_pairs_answer`
    with the same, undifferentiated empty list."""
    dry_run: bool = False
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one - checked against the *deciding* player
    (`_answer_declaration`'s own `engine_decision.player`), not any player
    named on this request."""


class PlayLandRequest(BaseModel):
    player: str
    card_id: UUID | None = None
    card: str | None = None
    dry_run: bool = False
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


class PassPriorityRequest(BaseModel):
    player: str
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


class ConcedeRequest(BaseModel):
    """CR 104.3a: a player may concede at any time, and leaves the game
    immediately. No priority is required and nothing can refuse it, so unlike
    every other action request this one has no `dry_run` - there is nothing to
    ask permission for."""

    player: str
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


class AdvanceRequest(BaseModel):
    """Who is advancing the turn - required, unlike `ActorRequest`, because
    `Game.advance` now authorizes the call against the active player (or
    their controller, CR 723.5) rather than merely logging who asked."""

    player: str
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


class BlockPair(BaseModel):
    blocker: UUID
    """The creature declaring the block."""
    blocked: UUID
    """The attacking creature it blocks."""


class DeclareBlockersRequest(BaseModel):
    player: str
    blocks: list[BlockPair] = Field(default_factory=list)
    """(blocker, blocked) pairs, in the order damage should be assigned
    through them. Empty is a legal answer, not a missing one - CR 509.1a's
    "if any" makes "declare no blockers" a real declaration, the one an
    autopilot run sends (`seatkit.pass_until`) when it has decided not to
    stop for combat; a `min_length=1` here used to make that answer
    inexpressible through this endpoint, the same gap `AttackRequest.attackers`
    had."""
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one - checked against the *deciding* player
    (`_answer_declaration`'s own `engine_decision.player`), not `player`
    above."""


class DeclaredPairRequest(BaseModel):
    """One `(creature, defender)` pair for the `declare_attackers`/
    `declare_blockers` kinds' `decide` answer (CR 508.1, CR 509.1) - the
    request-side twin of the engine's `machines.decisions.DeclaredPair`.

    `defender` is a card id (UUID string) or a player's username, the same
    token shape `CastRequest.targets`/`ActivateRequest.targets`/
    `DecideRequest.targets` already use for "a card or a player" - told apart
    the same way (`decisions.declared_pairs_answer`). A blocker always blocks
    a creature (CR 509.1a), so `defender` is a card id for every
    `declare_blockers` pair; an attacker's defender is overwhelmingly a
    player (CR 508.1b) but can also be a planeswalker or a battle's card id.
    """

    creature: UUID
    defender: str


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
    pairs: list[DeclaredPairRequest] = Field(default_factory=list)
    """The `declare_attackers`/`declare_blockers` kinds' answer (CR 508.1,
    CR 509.1) - the creatures declared and what each attacks or blocks, in
    declaration order. Empty is a real answer: "declare nobody", always legal
    ("if any"). The one field both declaration kinds share, the same way
    `choice` serves both `choose_a_quality` and `choose_a_mode` - see
    `DeclaredPairRequest`."""
    generation: int | None = None
    """Which standing question this answer is for -
    `pending_decision.generation`, echoed back.

    A resolution that asks two decisions hands the same seat a second
    question the instant the first is accepted, and a client that assembled
    its answer from the earlier read would otherwise post it against a
    question it never saw. Naming the generation makes that a refusal
    instead: the counter advances only when the pause slot's question is
    replaced (`repository.GameRepository.pause_generation`), so a mismatch
    means exactly "the question you read is not the one standing".

    `None` (the default) skips the check entirely, which is what every
    client that predates this field does. It is a guarantee a caller opts
    into, never a required field."""
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


class MulliganRequest(BaseModel):
    """One seat's CR 103.5 declaration: "a player who is dissatisfied with
    their initial hand may take a mulligan"."""

    player: str
    take: bool
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""
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
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


class PregameSettleRequest(BaseModel):
    """Admin-only: run the whole pregame (CR 103.5's mulligan process, CR
    103.6's opening-hand actions) forward, all at once, over every seat."""

    use_defaults: bool = False
    """`False` (the default): run as an ordinary command
    (`commands.run_command`) - pause on the first `mulligan`/
    `opening_hand_action`/bottoming `choose_cards` question the engine asks
    with no scripted answer, exactly like `advance` pauses on a combat
    declaration, and let a live seat answer it through `POST .../decide`.

    `True`: restore this endpoint's original, headless behaviour - answer
    every one of those questions with the engine's own default (keep the
    dealt hand, decline every CR 103.6 action, bottom the back of the hand)
    before the call returns, so the whole pregame settles in one request
    with nobody consulted (`commands.run_command_to_completion`)."""


# == Response views (T1) =====================================================
#
# These mirror serialize.py's dict-building functions field for field -
# serialize.py keeps returning plain dicts (nothing about its runtime
# behaviour changes here); each mirrored model is declared as a route's
# `response_model` in app.py, and FastAPI validates/documents against it.
#
# `visibility.redact_view` mutates that dict per-viewer before it leaves the
# process: it pops `libraries`/`sideboards` outright for anyone but the
# admin, shrinks `pending_decision` down to `{player, kind, prompt}` for
# anyone but the deciding player, can replace a face-down card's view with a
# much smaller stub (`_redact_facedown_card`), drops `decklist`/`cast_options`
# for anyone but their own seat, and adds a `you` key `state_view` itself
# never sets. None of that is a second shape a client must learn - it is the
# same shape with fields absent - so every field a redaction step can remove
# is modelled `| None = None` here rather than required, and `you` is on the
# model even though `serialize.state_view` never sets it.


class ChoiceView(BaseModel):
    """CR 614.12a's "as this enters, choose ..." answer
    (`serialize._card_view`'s `choice` key, from `models.card_state.Choice`)."""

    kind: str
    value: str


class TargetRef(BaseModel):
    """One pending-decision candidate or chosen target - a card, a player, or
    an ability on the stack named the way a card is (`decisions._target_view`).
    No `name`: unlike `TargetRefView` below, this is what a client answers
    `POST .../decide` with, not a display value."""

    kind: Literal["card", "player"]
    id: str


class TargetRefView(BaseModel):
    """One resolved target, with a display name for the UI
    (`serialize._target_refs`) - what `CardView.target_refs` and a stack
    ability's own `target_refs` carry, as opposed to `TargetRef` above."""

    kind: Literal["card", "player", "ability", "unknown"]
    id: str
    name: str


class TargetRequirementView(BaseModel):
    """One targeting requirement's shape (`affordances._profile_view`'s
    `requirements` entries) - how many targets it wants and, for a modal
    card, which mode it belongs to."""

    phrase: str
    count: int | None = None
    minimum: int | None = None
    unbounded: bool | None = None
    variable_count: bool | None = None
    mode: int | None = None
    mode_text: str | None = None


class DivisionView(BaseModel):
    """CR 601.2d's divided-effect shape (`affordances._profile_view`'s
    `division`) - Electrolyze's damage, distributed counters."""

    amount: int
    what: str


class TargetingProfileView(BaseModel):
    """What an announcement's targeting looks like right now
    (`affordances._profile_view`) - shared by `CardView.cast.targeting`,
    every `AbilityView.targeting`, and `AffordancesResponse` itself."""

    targets_anything: bool
    understood: bool
    minimum: int | None = None
    maximum: int | None = None
    requirements: list[TargetRequirementView]
    division: DivisionView | None = None


class ModeOptionView(BaseModel):
    number: int
    text: str


class ModalHintsView(BaseModel):
    """CR 700.2's modal shape (`affordances.cast_hints`'s `modes` key) -
    present only for a modal card."""

    instruction: str
    minimum: int
    maximum: int
    repeatable: bool
    understood: bool
    entwine_cost: str | None = None
    escalate_cost: str | None = None
    options: list[ModeOptionView]


class CastHintsView(BaseModel):
    """What casting this hand/exile card asks for - X, modes, targets
    (`affordances.cast_hints`)."""

    mana_cost: str | None = None
    needs_x: bool
    targeting: TargetingProfileView
    modes: ModalHintsView | None = None


class LoyaltyHintView(BaseModel):
    """CR 606.2's loyalty symbol (`affordances.ability_hints`'s `loyalty`
    key) - present only for a loyalty ability."""

    amount: int
    label: str


class AbilityView(BaseModel):
    """One activated ability this card could be asked to activate right now
    (`engine_api.list_abilities`, `serialize._card_view`'s `abilities`
    key) - its own index (what `ActivateRequest.ability_index` names), plus
    everything `affordances.ability_hints` reports about what activating it
    would ask for."""

    index: int
    text: str
    mana: bool
    cost: str | None = None
    needs_x: bool
    is_mana_ability: bool
    loyalty: LoyaltyHintView | None = None
    mana_options: list[str] | None = None
    targeting: TargetingProfileView


class OpeningHandOptionView(BaseModel):
    """One CR 103.6 action a seat's own opening hand offers right now
    (`opening.pregame_view`'s `opening_hand_options` entries)."""

    card_id: str
    source: str
    prompt: str


class PregameView(BaseModel):
    """What the game is waiting for in the pregame (`opening.pregame_view`),
    or `None` once CR 103.8's first turn has begun. `opening_hand_options`
    names cards in a hidden zone (CR 402.3), so `visibility._redact_pregame`
    empties every seat's entry but the viewer's own - never removes the key,
    so this needs no `| None` of its own beyond the whole field on
    `GameStateView`."""

    step: str
    awaiting_mulligan: list[str]
    hand_kept: dict[str, bool]
    mulligans_taken: dict[str, int]
    cards_to_bottom: dict[str, int]
    can_mulligan: dict[str, bool]
    opening_hand_actor: str | None = None
    opening_hand_options: dict[str, list[OpeningHandOptionView]]
    opening_hand_notices: list[str]


class SuspendUpkeepEntry(BaseModel):
    """One suspended card `engine_api.drive_suspend_upkeep` touched this
    upkeep - a time counter removed, and whether that was the last one."""

    card_id: str
    card: str
    time_counters: int
    cast_permission_granted: bool | None = None


class ExilePileView(BaseModel):
    """CR 406.4's pile marker (`serialize._exile_pile`) - which special
    action put this card in exile, whose it is, and the turn it landed."""

    marker: str
    owner: str | None = None
    turn_exiled: int | None = None
    linked_permanent_id: str | None = None
    """The permanent this card was exiled *with* - imprint, hideaway. Absent
    for every marker that links nothing, and a lookup that may find nothing
    even when present: nothing unlinks the two when the permanent leaves."""


class SpecialActionEntry(BaseModel):
    """One of `suspend`/`plot`/`foretell`'s current cost, if this hand card
    can take that action right now (`serialize._special_actions`)."""

    cost: str
    time_counters: int | None = None
    """Suspend only: how many time counters this card would enter exile with."""


class CyclingEntry(BaseModel):
    """One cycling ability this hand card can activate right now
    (`serialize._special_actions`'s `cycling` list)."""

    index: int
    keyword: str
    cost: str


class SpecialActionsView(BaseModel):
    """A hand card's live CR 116.2 special actions and cycling
    (`serialize._special_actions`). Every field is the "not right now" answer
    when absent - the card simply cannot take that action at the moment."""

    plot: SpecialActionEntry | None = None
    foretell: SpecialActionEntry | None = None
    suspend: SpecialActionEntry | None = None
    cycling: list[CyclingEntry] | None = None


class CastOptionView(BaseModel):
    """One CR 601.2b keyword permission a graveyard/exile card carries right
    now (`serialize._cast_option_view`) - flashback, mayhem, plot, foretell,
    madness, airbend, a printed CR 604.6 zone permission, or a keyword
    granted by another permanent's static ability."""

    keyword: str
    rule: str
    pays_mana_cost: bool
    cost: str | None
    usable_now: bool
    why_not: str | None
    granted_by: str | None = None


class CardView(BaseModel):
    """One card or permanent, wherever it sits (`serialize._card_view`).

    Almost everything past the first eight fields is `| None`, and that is
    not laxness: `visibility._redact_facedown_card` replaces a face-down
    exile/battlefield card's view with a stub carrying only `id`, `zone`,
    `controller`, `owner`, `tapped`, `face_up`, `name` (`"a face-down card"`),
    `card_name` (`None`) and `exile_pile` - every other key is simply absent
    from that dict. A required field here would make FastAPI's own response
    validation fail on exactly the redaction this api exists to perform.
    """

    id: str
    name: str
    card_name: str | None = None
    zone: str
    controller: str | None = None
    owner: str | None = None
    tapped: bool
    face_up: bool
    type_line: str | None = None
    mana_cost: str | None = None
    oracle_text: str | None = None
    counters: dict[str, int] | None = None
    damage: int | None = None
    summoning_sick: bool | None = None
    power: int | None = None
    toughness: int | None = None
    keywords: list[str] | None = None
    targets: list[str] | None = None
    target_refs: list[TargetRefView] | None = None
    image: str | None = None
    small_image: str | None = None
    art_crop: str | None = None
    abilities: list[AbilityView] | None = None
    choice: ChoiceView | None = None
    cast: CastHintsView | None = None
    special_actions: SpecialActionsView | None = None
    exile_pile: ExilePileView | None = None
    cast_options: list[CastOptionView] | None = None
    attached_to: TargetRefView | None = None
    printed_type_line: str | None = None
    printed_power: int | None = None
    printed_toughness: int | None = None


class StackSpellView(CardView):
    """A spell on the stack (`serialize._stack_view`) - a `CardView` plus the
    `kind` discriminator that tells it apart from `StackAbilityView`."""

    kind: Literal["spell"] = "spell"


class StackAbilityView(BaseModel):
    """An activated or triggered ability on the stack (CR 113.1c, CR 603) -
    `serialize._stack_view`'s other shape. No `CardView` fields: an ability
    is "an object that's not a card" (CR 603.3), so it carries its own
    source name and ability text instead."""

    kind: Literal["ability"] = "ability"
    id: str
    name: str
    controller: str | None = None
    text: str
    targets: list[str]
    target_refs: list[TargetRefView]
    zone: Literal["stack"] = "stack"


StackEntry = Annotated[StackSpellView | StackAbilityView, Field(discriminator="kind")]


class ManaPoolView(BaseModel):
    floating: dict[str, int]
    snow: dict[str, int]


class PlayerView(BaseModel):
    """One seat's own block (`serialize._player_view`). No `decklist` or
    `agent` field any more (2b's T8, map's S6): both were lobby-owned state
    read here on every state poll, the hot reverse seam the six-way cut
    cannot carry. Both moved to the lobby's own
    `GET /api/lobby/games/{id}/seats` (`decks_api.SeatDetail`), `decklist`
    redacted there per-viewer the same way it used to be redacted here - see
    `serialize._player_view`'s own docstring."""

    username: str
    life: int
    counters: dict[str, int]
    mana_pool: ManaPoolView
    hand_count: int
    library_count: int
    graveyard_count: int
    sideboard_count: int
    maximum_hand_size: int


class PlayPermissionView(BaseModel):
    """One live CR 601.3 dynamic cast/play grant (`serialize._play_permission_view`)."""

    card: CardView
    granted_by: str
    action: str
    pays_mana_cost: bool
    from_zone: str
    rule: str
    until_end_of_turn: bool
    immediate: bool
    granted_step: str | None = None
    expires: str
    expired: bool
    bottom_if_unused: bool
    usable_now: bool
    why_not: str | None = None


class PendingDecisionView(BaseModel):
    """The question a paused command is waiting on (`decisions.PendingDecision
    .to_view`), or - for any viewer but the deciding player and the admin -
    `visibility.redact_pending_decision`'s `{player, kind, prompt}` shrink of
    it (CR 101.4a: a choice made in a hidden zone stays face down for
    everyone else). Every field past `prompt` is `| None` for that reason,
    not because the engine ever leaves them unset for the deciding player.

    Named `PendingDecisionView` rather than `PendingDecision`: that name is
    already the (non-pydantic) dataclass in `decisions.py` this view mirrors,
    and the two are not interchangeable - `decisions.PendingDecision` also
    knows how to build itself `from_engine`, which is no part of a response
    contract."""

    player: str
    kind: str
    prompt: str
    generation: int | None = None
    source: str | None = None
    amount: int | None = None
    cards: list[str] | None = None
    targets: list[TargetRef] | None = None
    target_maximum: int | None = None
    target_minimum: int | None = None
    affirmative_is_legal: bool | None = None
    options: list[str] | None = None
    moved_label: str | None = None
    kept_label: str | None = None
    card_labels: list[str] | None = None
    exact: bool | None = None
    default_cards: list[str] | None = None
    card_details: list[CardView] | None = None
    """Only for the deciding player - the full view of exactly the ids
    `cards` names, resolved from zones `redact_view` otherwise strips
    entirely (`visibility.redact_pending_decision`)."""


class CombatAttackView(BaseModel):
    attacker: str
    defender: str
    blockers: list[str]


class CombatView(BaseModel):
    attacks: list[CombatAttackView]


class GameStateView(BaseModel):
    """The full board (`serialize.state_view`), as a viewer may see it after
    `visibility.redact_view` - see this section's own module-level note for
    why so much of this is `| None`. `libraries`/`sideboards` are the two
    fields redaction removes outright rather than merely emptying (CR 401.2:
    nobody may look at a library, not even their own); `you` is the one field
    `redact_view` *adds* that `state_view` itself never sets."""

    game_id: str
    pending_decision: PendingDecisionView | None = None
    turn_number: int | None = None
    step: str
    phase: str | None = None
    pregame: PregameView | None = None
    active_player: str | None = None
    priority_holder: str | None = None
    players: list[PlayerView]
    battlefield: list[CardView]
    stack: list[StackEntry]
    exile: list[CardView]
    exile_pile: list[CardView]
    play_permissions: dict[str, list[PlayPermissionView]]
    command: list[CardView]
    hands: dict[str, list[CardView]]
    graveyards: dict[str, list[CardView]]
    libraries: dict[str, list[CardView]] | None = None
    sideboards: dict[str, list[CardView]] | None = None
    winner: str | None = None
    drawn: bool
    losers: list[str]
    conceded: list[str]
    combat: CombatView
    you: str | None = None
    """Not set by `serialize.state_view` - `visibility.redact_view` stamps
    this onto the view for any non-`None` viewer, after every other field is
    already settled."""


class NewGameResponse(GameStateView):
    """`POST /api/games`'s own response - the full, unredacted state view
    (the creator gets every seat's uid and the admin key in one call, so a
    filtered view would hide nothing a redaction step is for) plus the seat
    credentials themselves."""

    seat_uids: dict[str, str]
    admin_key: str


class AddedCardInfo(BaseModel):
    card_id: str
    name: str
    modern_status: str
    warning: str | None = None


class PutCardResponse(GameStateView):
    added: AddedCardInfo


class PatchCardResponse(GameStateView):
    patched: str


class SetStepResponse(GameStateView):
    suspend_upkeep: list[SuspendUpkeepEntry]


class ActionOutcome(BaseModel):
    """The response shape of every action-mutating route
    (`app._respond`/`app.audit_outcome`) - `engine_api.Outcome.body()` plus
    the audit log's own `log_seq`.

    `allowed` is `None` exactly when the action paused on an unanswered
    decision (`paused=True`, `pending_decision` set) rather than completing
    or being refused outright. Fields beyond the five declared here are
    action-specific (`Outcome.detail` - e.g. `cost`, `tapped`, `spell_id`,
    `ability_id`, `card_id`, `attackers`, `mana_ability`, `ability`) and are
    deliberately **not** enumerated as named fields: `engine_api.Outcome
    .detail` is untyped by design - each of its call sites across
    `engine_api.py` builds a different key set for a different action, and
    naming them all here would either claim a field a given route's response
    never actually carries, or force one implausibly wide union. This model
    instead declares the fields every mutating route's response shares and
    lets the rest through as additional, unnamed properties
    (`extra="allow"`) - the same honesty `serialize.state_view`'s own
    dynamically-keyed fields get, extended to this genuinely per-action
    shape. A consumer that needs one action's exact detail keys should pin
    against that route's own example response, not against this component.
    """

    model_config = ConfigDict(extra="allow")

    allowed: bool | None
    reason: str | None = None
    paused: bool = False
    pending_decision: PendingDecisionView | None = None
    log_seq: int | None = None


class MoveCardSuccessResponse(GameStateView):
    """`move_card`'s response when the move was allowed:
    `{**state_view(...), **outcome.body()}` plus `log_seq`. When the move was
    refused, `move_card` returns bare `ActionOutcome` instead - no state at
    all - so the route's real response type is
    `ActionOutcome | MoveCardSuccessResponse`, not this model alone."""

    model_config = ConfigDict(extra="allow")

    allowed: bool
    reason: str | None = None
    log_seq: int | None = None


class HealthResponse(BaseModel):
    status: str
    protocol_version: str


class SeatResponse(BaseModel):
    player: str


class MintSeatRequest(BaseModel):
    """2b's T8, the seat back door's mint half. `player` must already be
    registered on the game (`Game.players`) - this mints a fresh credential
    for an existing seat, it does not add a player to the game."""

    player: str = Field(min_length=1)


class MintSeatResponse(BaseModel):
    uid: str
    player: str


class SeatLookupResponse(BaseModel):
    """2b's T8, the seat back door's lookup half: `GET /api/seats/{uid}`
    resolves a uid to its game with no game id known in advance - the
    capability `store.seat_uids` gave an in-process caller for free and no
    out-of-process one (a test, or a shared mcp server, M2) can reach."""

    game_id: str
    player: str


class GameSummary(BaseModel):
    game_id: str
    players: list[str]
    step: str | None = None


class ListGamesResponse(BaseModel):
    games: list[GameSummary]


class GameSummaryScalars(BaseModel):
    """One game's row from `GET /api/games/summary` (2b's T8, map's S2) -
    exactly the scalars a lobby list needs and nothing a full `state_view`
    would cost to build: no card views, no Scryfall images, one bulk call
    for every id a poll asks about instead of one `state_view` per game."""

    game_id: str
    is_over: bool
    turn_number: int | None = None
    active_player: str | None = None
    phase: str | None = None
    seat_count: int


class GameSummariesResponse(BaseModel):
    summaries: list[GameSummaryScalars]


class GameLogResponse(BaseModel):
    """`GET .../log`'s response. `log` entries are left as `dict[str, Any]`
    deliberately: `audit.entries`' `detail` payload is exactly as
    per-action-shaped as `ActionOutcome`'s own extra fields (T10's
    `audit.record(**keys)` writes whatever the calling route passed), and a
    log entry additionally carries an optional full board `snapshot`
    (`serialize.snapshot`) - modelling either faithfully is a second task's
    worth of work this one does not need, since neither is one of the four
    components T1 requires by name (the request models, `PendingDecision`,
    the state view, the outcome shape)."""

    log: list[dict[str, Any]]
    last_seq: int


class ReviewEntry(BaseModel):
    """One ledger row as a declaration's pairing carries it - no `snapshot`,
    which lives once in `GameReviewResponse.entries` (see `review.py`)."""

    seq: int
    ts: str | None = None
    turn: int | None = None
    step: str | None = None
    phase: str | None = None
    action: str
    player: str | None = None
    """Whose row this is - lifted out of `detail` for readability, and still
    in `detail` too."""
    reasoning: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    """The action's own per-route keys - as per-action-shaped as
    `GameLogResponse.log`'s, and left `dict[str, Any]` for the same reason."""


class ReviewDeclaration(BaseModel):
    """One `plan_declared` row, paired with what happened next.

    Nothing on this model grades the prediction - no score, no hit flag, no
    verdict. See `review.py` for why the raw pairing is the thing a sceptic
    cannot argue with.
    """

    seq: int
    ts: str | None = None
    turn: int | None = None
    turns_taken: int | None = None
    step: str | None = None
    phase: str | None = None
    player: str | None = None
    board_read: str | None = None
    my_role: str | None = None
    plan: str | None = None
    prediction: OpponentPrediction | None = None
    did: list[ReviewEntry] = Field(default_factory=list)
    """What the declaring seat actually did, its own reasoning beside each."""
    opponent_did: list[ReviewEntry] = Field(default_factory=list)
    """What the opponent actually did in the same window."""


class GameReviewResponse(BaseModel):
    """`GET /api/games/{game_id}/review` - post-game only, and never open."""

    game_id: str
    players: list[str]
    created_at: str | None = None
    ended_at: str | None = None
    """When the game was ended from the lobby or deleted, `None` for one that
    simply finished."""
    is_over: bool
    winner: str | None = None
    drawn: bool = False
    ended_seq: int | None = None
    """The `game_over` row's own `seq`, `None` for a game that was ended
    before it finished."""
    declarations: list[ReviewDeclaration] = Field(default_factory=list)
    entries: list[dict[str, Any]] = Field(default_factory=list)
    """The whole ledger, oldest first, each row carrying its own
    full-information `snapshot` - the frames a reviewer renders. Left
    `dict[str, Any]` exactly as `GameLogResponse.log` is, and for the same
    reason."""


class DeleteGameResponse(BaseModel):
    deleted: str


class CardNamesResponse(BaseModel):
    suggestions: list[str]


class CardLookupResponse(BaseModel):
    """`GET /api/cards`/`GET /api/cards/{name}` (`scryfall.lookup_card`)."""

    requested: str
    name: str | None = None
    scryfall_id: str | None = None
    images: dict[str, str] = Field(default_factory=dict)
    modern_status: str
    modern_legal: bool
    ok: bool
    error: str | None = None
    card: Card | None = None
    """Only when `ok` - the raw fetch validated against the engine's own
    `Card` model, reused directly rather than re-declared here."""


class TapCardResponse(BaseModel):
    """`POST .../cards/{card_id}/tap` - `audit_outcome` only, no state view
    merged in. `tapped` is present only on the success outcome
    (`engine_api.Outcome(True, detail={"tapped": ...})`); the refusal branch
    (`engine_api.refused(exc)`) carries no detail at all."""

    allowed: bool
    reason: str | None = None
    tapped: bool | None = None
    log_seq: int | None = None


class AdvanceResponse(GameStateView):
    """`POST .../advance`'s paused-or-completed response - always the full
    state view. The one branch with none at all (the engine's own refusal,
    caught before `_run_command` ever attempted anything) is bare
    `ActionOutcome` instead, so the route's real response type is
    `ActionOutcome | AdvanceResponse`."""

    allowed: bool | None = None
    paused: bool = False
    advanced_to: str | None = None
    suspend_upkeep: list[SuspendUpkeepEntry] | None = None
    log_seq: int | None = None


class StateBasedActionsResponse(GameStateView):
    """`POST .../state-based-actions` - always the full state view, both
    paused and completed."""

    allowed: bool | None = None
    paused: bool = False
    destroyed: list[str] | None = None
    changed: list[str] | None = None
    log_seq: int | None = None


class ResolveTopResponse(GameStateView):
    """`POST .../resolve-top` - always the full state view. `reason` is set
    (with `resolved: None`) when `engine_api.resolve_top` itself refused
    (no legal object to resolve), which is still a `RunStatus.COMPLETED`
    result, not a paused or a raised one."""

    resolved: str | None = None
    zone: str | None = None
    reason: str | None = None
    allowed: bool | None = None
    paused: bool = False
    log_seq: int | None = None


class PregameSettleResponse(GameStateView):
    """`POST .../pregame/settle` - always the full state view, both paused
    and completed (admin-only, T7b)."""

    allowed: bool | None = None
    paused: bool = False
    log_seq: int | None = None


class ConcedeResponse(BaseModel):
    """`POST .../actions/concede` - its own shape (CR 104.3a: nothing can
    refuse a concession, so this never carries `reason`), not
    `_respond`/`ActionOutcome`."""

    allowed: bool | None = None
    paused: bool = False
    pending_decision: PendingDecisionView | None = None
    winner: str | None = None
    log_seq: int | None = None


class PassPriorityResponse(BaseModel):
    """`POST .../actions/pass-priority`'s own shape - never merges the state
    view (unlike `advance`, which moves the turn marker, passing priority
    only ever resolves the stack's top or ends the step)."""

    allowed: bool | None = None
    reason: str | None = None
    paused: bool = False
    pending_decision: PendingDecisionView | None = None
    outcome: str | None = None
    """`magic_cards_model.machines.priority.PriorityOutcome`'s value -
    `"resolve_top"` or `"end_step"`."""
    settled_through: list[str] | None = None
    log_seq: int | None = None


class DecideResponse(BaseModel):
    """`POST .../decide` - its own `accepted`/`reason` shape, not
    `allowed`/`ActionOutcome`: `decide` answers a standing question rather
    than attempting a new action, so "was this answer accepted" is the
    question, not "was this allowed"."""

    accepted: bool
    reason: str | None = None
    paused: bool = False
    pending_decision: PendingDecisionView | None = None
    log_seq: int | None = None


class ReportProblemResponse(BaseModel):
    recorded: bool


class MulliganResponse(BaseModel):
    """`POST .../pregame/mulligan` (`opening.declare_mulligan`)."""

    declared: bool
    step: str
    pregame: PregameView | None = None


class OpeningHandActionResponse(BaseModel):
    """`POST .../pregame/opening-hand/action` (`opening.take_opening_hand_action`)."""

    taken: str
    step: str
    pregame: PregameView | None = None


class OpeningHandFinishResponse(BaseModel):
    """`POST .../pregame/opening-hand/finish` (`opening.finish_opening_hand_actions`,
    settled through any priorityless step by `app._settled`)."""

    step: str
    pregame: PregameView | None = None
    settled_through: list[str] | None = None


class LegalActionsResponse(BaseModel):
    """`GET .../legal-actions` (T11) - the engine's own `Action` model,
    reused as-is rather than re-shaped: `get_legal_actions` returns exactly
    `[action.model_dump(mode="json") for action in actions]`, and
    `records.LegalActionsBefore.actions` (surfaced redacted, per-viewer, as
    `LogEntry.legal_actions_before`) is the identical dump."""

    actions: list[Action]


class CandidateView(BaseModel):
    """One object `affordances.candidates` found legal for at least one of
    an announcement's targeting requirements (`affordances._label` plus
    which requirement slots it fills)."""

    id: str
    kind: str
    name: str
    zone: str | None = None
    controller: str | None = None
    requirements: list[int]


class AlternativeCostView(BaseModel):
    """One CR 118.9 alternative cost this card could be cast under
    (`affordances.alternative_costs`), available or not."""

    index: int
    source: str
    rule: str
    source_name: str
    keyword: str | None = None
    description: str
    cost: str | None = None
    requires: int
    available: bool
    unavailable: str | None = None


class ObjectCostCandidateView(BaseModel):
    """One object offered against a convoke/delve/crew cost
    (`affordances.object_costs`'s `eligible()`) - `CandidateView`'s shape
    minus `requirements` (this is a payment offer, not a targeting slot)
    plus an optional `power`, present only for a battlefield candidate."""

    id: str
    kind: str
    name: str
    zone: str | None = None
    controller: str | None = None
    power: int | None = None


class ConvokeCostView(BaseModel):
    rule: str
    candidates: list[ObjectCostCandidateView]


class DelveCostView(BaseModel):
    rule: str
    candidates: list[ObjectCostCandidateView]


class CrewCostView(BaseModel):
    rule: str
    number: int
    candidates: list[ObjectCostCandidateView]


class ObjectCostsView(BaseModel):
    """Which objects could be spent on this card's cost, and for what
    (`affordances.object_costs`) - only the keys that apply are ever
    present."""

    convoke: ConvokeCostView | None = None
    delve: DelveCostView | None = None
    crew: CrewCostView | None = None


class AffordancesResponse(TargetingProfileView):
    """`GET .../affordances/{card_id}` - `affordances.candidates`'s own
    `TargetingProfileView` fields plus its candidate list, and (for a cast,
    not an ability) the alternative costs and object-cost offers this card
    carries right now."""

    candidates: list[CandidateView]
    error: str | None = None
    """Set instead of a normal profile when building one itself failed
    (`affordances.candidates`'s `IndexError`/`ValueError` catch) - the
    profile fields are then `_no_targets()`'s empty defaults, `candidates`
    is `[]`, and this names why."""
    alternative_costs: list[AlternativeCostView] | None = None
    object_costs: ObjectCostsView | None = None
