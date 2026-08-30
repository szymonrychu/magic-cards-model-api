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


class PassPriorityRequest(BaseModel):
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
    non-empty string. `None` (the default) is refused, matching `accept`."""
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
