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
from magic_cards_model.models.action import ActionKind
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    WithJsonSchema,
    model_validator,
)
from pydantic_core import PydanticCustomError


class RequestModel(BaseModel):
    """The base every request body in this api is built on, and it exists for
    one line: `extra="forbid"`.

    Pydantic's default is to **discard** a key a model does not declare,
    silently, and that turned a request field into something a caller could
    claim without the route ever honouring it. `game_access._advisor_may`
    permitted a POST carrying `dry_run: true` - a probe, changing nothing -
    but only nine routes declare `dry_run`, so on every other shape pydantic
    threw the key away and the route committed for real. `MintSeatRequest.
    model_validate({"player": "alice", "dry_run": True})` produced
    `player='alice'` and nothing else, and `POST .../seats` then handed the
    caller a fresh, unrestricted seat credential.

    The gate reads the route's own declared shape now rather than the body
    (`game_access.DRY_RUN_ROUTES`), and that is the fix for *that* hole.
    This is the invariant that catches the next one: a caller that sends a
    field the route cannot honour gets a 422 saying so, instead of a 200
    that did something other than what it asked for. It applies to the
    shape a route added tomorrow declares, without anybody listing it
    anywhere.

    Two things it cannot cover, both worth knowing:

    - `POST .../cards/{card_id}/tap` takes a raw `dict[str, bool]`, not a
      model, so no `model_config` reaches it. `{"dry_run": true}` there
      simply defaults `tapped` to `True` and taps for real - the gate is the
      only thing that can refuse it.
    - `uid`/`key` are read off the raw request by `game_access._credentials`
      rather than declared as parameters, precisely so no route's OpenAPI
      shape changes because of the gate. A body-borne credential therefore
      *is* an undeclared field on most shapes - so the four models whose
      route carries one (`DeclarePlanIn`, `ReflectionIn`, and
      `ai_seat_api.StartAiSeat`) declare `uid` for real, and always did.
    """

    model_config = ConfigDict(extra="forbid")


class NewPlayer(RequestModel):
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


class NewGame(RequestModel):
    players: list[NewPlayer] = Field(min_length=1)
    active_player: str | None = None
    """Defaults to the first player listed."""
    step: str = "precombat_main"
    """Step the game starts in. Any Step value."""


class PutCard(RequestModel):
    card: str
    """A card name. Always fetched from Scryfall (cache first)."""
    player: str
    zone: str = "hand"
    position: str | None = None
    """For the library: "top" (default) or "bottom"."""
    tapped: bool = False
    face_up: bool = True


class MoveCard(RequestModel):
    to_zone: str
    player: str | None = None
    """The player whose zone receives the card (their hand, library, ...)."""
    controller: str | None = None
    """New controller, for shared zones (battlefield, exile)."""
    position: str | None = None
    tapped: bool | None = None
    face_up: bool | None = None


class PatchCard(RequestModel):
    tapped: bool | None = None
    face_up: bool | None = None
    damage: int | None = None
    summoning_sick: bool | None = None
    counters: dict[str, int] | None = None
    """Counter kind (e.g. "+1/+1", "loyalty") -> count to set."""


class PatchPlayer(RequestModel):
    life: int | None = None
    counters: dict[str, int] | None = None
    """Counter kind (e.g. "poison", "energy") -> count to set."""


class ManaPoolUpdate(RequestModel):
    empty: bool = False
    add: dict[str, int] | None = None
    """Color letter ("W", "U", "B", "R", "G", "C") -> amount."""
    set: dict[str, int] | None = None
    """Replace the whole pool with this."""


class BoardRead(RequestModel):
    """P4: the ledger position a seat decided against, named by the seat.

    **Optional, and it stays optional.** Every caller that existed before
    this field - the browser above all - sends no `expected_seq` and is
    refused on nothing new; `None` means "I am not naming a board", which is
    exactly the behaviour this api always had.

    When it *is* given, `records.require_current_board` compares it to
    `audit.last_seq(game_id)` before anything else runs, and refuses the
    call outright when the two differ. That is what makes the corpus answer
    the question it could not answer before: how many of a seat's refusals
    were races (it acted on a board that had already moved) and how many
    were genuine misreads of the rules. The two used to be
    indistinguishable - losing a race produced `claude does not have
    priority, you does`, prose a reader can only guess at.

    **`detail.stale` on the ledger row carries all three answers**, and
    naming the field is only half of it - `scripts/eval_report.py` reads
    every one:

    - `true` - the board had moved; the engine was never asked.
    - `false` - this seat named the board it decided against, that board was
      current, and the engine judged the action illegal on it. A rules error
      only when the api had not itself offered that action for that board
      (`detail.offered`, `records.offered_action`) - a refusal of an action
      this api's own enumeration listed is not the seat misreading anything.
    - absent - the caller sent no `expected_seq`, so nothing can be said
      about which of the two it was.

    The `false` case is the reason sending this field is worth a seat's
    while beyond the refusal itself: without it every rules refusal is
    indistinguishable from an unmeasured one, and `eval_report`'s rules
    bucket is structurally zero (MEMORY.md 2026-09-12).

    It is a *ledger* position and not a game version on purpose: `records
    .seq` is the number every answer already hands a seat back
    (`log_seq`, `GET .../log`'s `last_seq`, `seatkit.Seat.recent_log`), so
    a seat names the board it read using the number it was already given,
    with nothing new to track.
    """

    expected_seq: int | None = None


class StepUpdate(BoardRead):
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


class ActorRequest(BoardRead):
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

#: The most characters an advisor's rationale may run to (bd1). The buddy's
#: `reasoning` is read by a human in a small panel beside the game, and the
#: production buddy was writing ~1,000-character paragraphs that nobody reads
#: mid-turn. Enforced on `AdviceIn.reasoning` as a typed 422
#: (`reasoning_too_long`) the buddy can act on, pre-checked client-side by
#: `seatkit.ADVICE_REASONING_MAX_CHARS` (kept equal by hand, pinned by
#: `tests/test_advise_mode.py`), and clipped to in the UI
#: (`ui/advice_model.js`'s `RATIONALE_MAX_CHARS`) for rows older than the cap.
ADVICE_REASONING_MAX_CHARS = 240


def _short_rationale(value: str) -> str:
    """Refuse a rationale over `ADVICE_REASONING_MAX_CHARS`, typed and saying
    what to do about it - an agent's only feedback is the response body."""
    if len(value) > ADVICE_REASONING_MAX_CHARS:
        raise PydanticCustomError(
            "reasoning_too_long",
            "reasoning is {length} characters and an advice rationale is capped at "
            "{max_chars}: shorten it to one or two plain sentences and propose again",
            {"length": len(value), "max_chars": ADVICE_REASONING_MAX_CHARS},
        )
    return value


#: A `Stated` rationale no longer than `ADVICE_REASONING_MAX_CHARS`, with the
#: cap written into the contract as `maxLength`.
Rationale = Annotated[
    Stated,
    AfterValidator(_short_rationale),
    Field(json_schema_extra={"maxLength": ADVICE_REASONING_MAX_CHARS}),
]


class OpponentPrediction(RequestModel):
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


class DeclarePlanIn(RequestModel):
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
    for_turn: Literal["current", "next"] = "current"
    """Which turn this declaration is about. `"current"` (the default) is
    the turn already under way - the only shape this ever took before this
    field existed. `"next"`, given while it is NOT this seat's own turn,
    plans ahead: the row is written against this seat's own *next* turn
    (`records.next_own_turn`, which asks the engine's own `TurnMachine.
    begin_next_turn` rather than re-deriving turn order here), so an agent
    can think during the opponent's turn instead of holding priority in its
    own upkeep to do it - the failure this field exists to fix (live game
    2026-09-14: a ~110s plan written in upkeep with the human waiting on
    it). `"next"` while this seat's own turn is already in progress is
    refused - `app.declare_plan` answers 409 `own_turn_in_progress`; one
    plan per turn, and `"current"` is how that turn gets its own."""


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
    """The turn this plan covers, as the ledger stamped it - the current
    turn's own `turn_number` for `for_turn: "current"`, or the computed
    target turn's `turn_number` for `for_turn: "next"` (`records.
    next_own_turn`'s own `TurnMachine.turn_number`, which is `None` only
    when there is no turn under way at all to plan against)."""
    for_turn: Literal["current", "next"] = "current"
    """Which turn `turn_number` above names - see `DeclarePlanIn.for_turn`."""


class ReflectionIn(RequestModel):
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


class AdviceIn(RequestModel):
    """`POST /api/games/{game_id}/advice` - one action an **advisor** thinks
    its human should take, recorded and never performed.

    An advisor credential (`repository.Advisor`) is read-only by
    construction: `game_access.py` refuses every mutating POST it makes
    except this one and `/report`. So this is not a convenience - it is the
    only way a buddy pod reaches the game it is watching at all, and the
    reason it is a *ledger row* rather than anything held in memory is that
    the api is multi-replica: a proposal kept in one process would be
    invisible to the replica the human's next poll happens to hit.

    `method`/`path`/`body` are the **request the human's client will post
    verbatim** once it agrees, built by `seatkit` (which already knows how to
    shape every action) rather than hand-written by a model - see
    `seatkit.Seat._advise_answer`. `body` names the human as `player` and
    carries no `dry_run`: an approved proposal is a real action.

    There is deliberately no `summary` field here. What the proposal *says*
    is derived server-side from `body` and the live game
    (`records.advice_summary`), so it cannot disagree with the request it
    describes - an agent-written summary naming a different card from the one
    `body` would actually cast is exactly the failure that shape invites.

    `reasoning` is the half that *is* the agent's own words, and the half the
    human actually reads before deciding. Required and non-blank: a proposal
    with no argument for it is a button, not advice.
    """

    player: Stated
    """The human seat being advised - the `player` an approved `body` acts
    as, and (via `visibility.resolve_viewer`) the only viewer other than that
    seat's own advisor who may read this row back."""
    method: Stated
    """The HTTP method of the request being proposed - `POST` for every
    action shape there is; carried rather than assumed so the row stays a
    complete, replayable description of a request."""
    path: Stated
    """The request path, e.g. `/api/games/{id}/actions/cast`."""
    body: dict[str, Any] = Field(default_factory=dict)
    """The exact ready-to-post request body: `player` is the human, and
    `dry_run` is absent."""
    reasoning: Rationale
    """Why, in the advisor's own words. This is the rationale the human
    reads: one or two plain sentences, at most `ADVICE_REASONING_MAX_CHARS`
    (240) characters. A longer one is refused with a 422 of type
    `reasoning_too_long` and nothing is recorded."""
    validated: bool = False
    """Whether the engine itself has already agreed this action is legal.
    `True` only for the 7 of 12 action shapes that carry `dry_run`
    (`seatkit.DRY_RUN_SHAPES`), which an advisor probes before proposing.
    `False` says "nobody has checked this" - the honest answer for the other
    5, whose shapes have no way to ask, and which an advisor therefore must
    not send at all."""


class AdviceResponse(BaseModel):
    """`POST .../advice`'s answer. No state view: recording a proposal
    mutates no `Game`, so there is nothing about the board it could have
    changed - the same reason `DeclarePlanResponse` carries none."""

    proposed: bool
    log_seq: int
    """The `records` row this proposal became - the `seq` the human's client
    passes back to `POST .../advice/{seq}/approve`."""
    player: str
    summary: str
    """What the server read this proposal as (`records.advice_summary`) -
    returned as well as recorded so the advisor can see whether the body it
    built says what it meant."""
    validated: bool
    based_on_seq: int
    """The ledger head this proposal was recorded against. A command that
    landed between the advisor's read and this row makes the proposal
    born-stale, which is the safe direction: the human's own action carries
    `expected_seq` and is refused rather than applied to a board that moved."""


class AdviceApproveIn(RequestModel):
    """`POST /api/games/{game_id}/advice/{seq}/approve` - the human saying
    yes to one proposal, by its ledger `seq`.

    Human uid only. An advisor's own uid is refused by `game_access.py`
    before this route ever runs (the path is neither `/advice` nor `/report`
    and the body claims no `dry_run`), and the route checks the same thing
    again against `store.player_for_uid`: an advisor must never be able to
    approve its own advice, and that must hold whether or not the gate is
    the thing enforcing it on any given day.
    """

    player: Stated
    """The approving human - must be the advised `player` of the row at
    `seq`, and must be the seat the caller's own uid opens."""


class AdviceApprovedResponse(BaseModel):
    """`POST .../advice/{seq}/approve`'s answer. Mutates no `Game` either."""

    recorded: bool
    log_seq: int
    """The `advice_approved` row this became."""
    advice_seq: int
    """The `advice` row it approves."""
    player: str


class ScriptsRegisterIn(RequestModel):
    """`PUT /api/games/{game_id}/seats/{uid}/scripts` - the play-script corpus
    a seat Job runs, registered under the uid it runs with.

    `scripts` is deliberately a list of plain objects here rather than
    `playscripts.schema.Play`: every script is judged by
    `playscripts.schema.play_errors`, which answers with the coded errors
    (`unknown-field`, `forbidden-kind`, ...) mtg-decks' own checker shares,
    and a refusal the runner can act on is worth more than pydantic's
    generic 422. The shape each must satisfy is
    `contract/playscript.schema.json`.
    """

    deck_slug: Stated
    """The mtg-decks deck directory these scripts came from."""
    guides_revision: Stated
    """The guides checkout revision they were read at."""
    scripts: list[dict[str, Any]]
    inputs: Annotated[
        Any,
        WithJsonSchema(
            {"type": "array", "items": {"type": "object", "additionalProperties": True}}
        ),
    ] = Field(default_factory=list)
    """The deck's declared inputs, `decks/<slug>/plays/inputs.json`'s
    `inputs` list, judged by `playscripts.inputs` and refused as
    `malformed-inputs`. Empty when the deck declares none. Typed `Any` for the
    same reason as `scripts`: a value that is not a list of objects reaches
    `records.inputs_errors` and is refused in its words, not pydantic's."""


class ScriptsRegisteredResponse(BaseModel):
    """`PUT .../seats/{uid}/scripts`'s answer. The corpus itself is not echoed
    back; `GET` of the same path reads it."""

    registered: bool
    uid: str
    deck_slug: str
    guides_revision: str
    count: int
    """How many scripts the corpus holds."""
    registered_at: str


class ScriptsCorpusResponse(BaseModel):
    """`GET .../seats/{uid}/scripts` - the registered corpus, readable by that
    uid, the seat's advisor and the admin key only."""

    uid: str
    deck_slug: str
    guides_revision: str
    scripts: list[dict[str, Any]]
    inputs: list[dict[str, Any]]
    """The declared inputs registered beside `scripts`; empty for none."""
    registered_at: str


class ScriptRunIn(RequestModel):
    """`POST /api/games/{game_id}/script-runs` - one script run, reported by
    the runner at its `start` and again at its `finish`.

    Not a game action: it mutates no `Game`, and the moves a script makes go
    through the action routes, which gate them. So none of
    `records.require_current_board`, `require_reasoning` or `require_plan`
    applies, and there is no `expected_seq` or `reasoning` here to read.

    `start` is refused with `code: unknown_script` when `script_id` is not
    in the corpus registered under the uid that runs it, and with exit
    `already_ran` when this `seat` already started (or finished) `script_id`
    at this `turn` and `step`. `finish`
    writes one `script_run` row carrying every field below.
    """

    phase: Literal["start", "finish"]
    run_id: Stated
    """The runner's own id for this run."""
    parent_run_id: str | None = None
    """The run that chained into this one through `next`, when one did."""
    seat: Stated
    """The player this run plays for - the advised human, for an advisor."""
    role: Literal["seat", "advisor"]
    """Which kind of uid the runner holds: a seat uid or an advisor uid."""
    advise: bool = False
    """Whether the moves were proposed as advice rather than taken."""
    script_id: Stated
    script_sha: str | None = None
    """A digest of the script as registered, so a row names the exact text."""
    guides_revision: str | None = None
    priority: int | None = Field(default=None, ge=0)
    opposing_archetype_asserted: str | None = None
    """The `opposing_archetype` input the runner believed when it matched."""
    turn: int = Field(ge=0)
    """The game's `turn` number the script matched at."""
    step: Stated
    """The step the script matched at."""
    seq_start: int | None = Field(default=None, ge=0)
    """The ledger position the run started from."""
    seq_end: int | None = Field(default=None, ge=0)
    """The ledger position the run ended at."""
    moves_done: int | None = Field(default=None, ge=0)
    moves_total: int | None = Field(default=None, ge=0)
    exit: str | None = None
    """How the run ended, in the runner's own vocabulary. Required on
    `finish`."""
    stop_reason: str | None = None
    refusal_text: str | None = None
    """The engine's refusal, verbatim, when a move was refused."""
    resumes: int | None = Field(default=None, ge=0)
    """How many times the run resumed across a paused decision."""
    wall_ms: int | None = Field(default=None, ge=0)
    reapplicable_after: str | None = None
    """When the runner may try this script again."""

    @model_validator(mode="after")
    def _a_finish_names_its_exit(self) -> ScriptRunIn:
        if self.phase == "finish" and not (self.exit and self.exit.strip()):
            raise ValueError("a finish must name its exit")
        return self


class ScriptRunResponse(BaseModel):
    """`POST .../script-runs`'s answer."""

    recorded: bool
    phase: str
    run_id: str
    log_seq: int
    """The ledger row this report became."""


class ReportProblemRequest(RequestModel):
    player: str
    text: str = Field(min_length=1)
    """What the player thinks is wrong, written by the player."""


class ManaSource(RequestModel):
    card_id: UUID
    colors: list[str] | None = None
    """Which colors to tap it for; defaults to the ability's own production."""
    ability_index: int | None = None
    """Which of this source's mana abilities to tap it for (CR 605.1a), in the
    same index space `AbilityView.index` publishes and `ActivateRequest.
    ability_index` takes - one space, one meaning, everywhere.

    Only a source that prints more than one needs it, and every such printing
    in this pool pairs a plain `{T}: Add ...` with a CR 106.6-restricted one:
    Eldrazi Temple's `{C}` against its `{C}{C}`, Cavern of Souls's and
    Delighted Halfling's `{C}` against their one mana of any colour, Sunken
    Citadel's one mana of the chosen colour against two of it. Naming the index
    is how a player says which of the two they meant, and it is what lets
    `engine_api.pay_with` hand `CostPayment.tap_for_mana` that ability's own
    restriction rather than let the engine re-derive one from what the mana
    turned out to be (`machines.cost.restriction_for_production`).

    Sunken Citadel is the pair where that matters, and it is the only one:
    inference matches on production, so `{C}` tells Cavern's and Delighted
    Halfling's two abilities apart on its own ("any color" is CR 106.1a's five
    colours, and colorless is a type of mana that is not a color, CR 106.1b).
    Both of Sunken Citadel's read "the chosen color", which
    `restriction_for_production` resolves with no board at all - so neither
    matches, and its restricted mana is reported as unrestricted.

    `None` - absent, or explicitly null - is exactly today's behaviour: the
    other two shapes of this model are untouched by this field's existence.
    """


class CastRequest(BoardRead):
    player: str
    card_id: UUID | None = None
    card: str | None = None
    """Card name, used when no card_id is given: first match in from_zone."""
    from_zone: str = "hand"
    spell_face: str | None = None
    """Which face this cast announces - a half of a split card (CR 709.3) or
    an adventurer card's Adventure (CR 715.3) - by that face's own printed
    name: `"Tear"` for Wear // Tear's back half, `"Swift End"` for Murderous
    Rider's Adventure.

    **The value is the engine's own.** It is exactly the string the engine put
    in `Action.spell_face` on the row this cast is taking, so a seat hands back
    what it was offered, unmodified and untransformed - post `GET
    .../legal-actions`'s `"Tear"` straight into this field. That round trip is
    the whole point of naming the field after the engine's own: a caller that
    had to convert what it read into some parallel vocabulary of this api's
    invention is a caller that will convert it wrong, and there is no second
    place to look up the spelling.

    `None` - absent, or explicitly null - is the card's plain front-face cast,
    which is what every caller written before this field existed sends, what a
    single-faced card only ever has, and how an adventurer card's creature
    half is cast (CR 715.3's other branch names no face at all).

    CR 709.3a: "Only the chosen half is evaluated to see if it can be cast";
    CR 715.3a says the same of an Adventure, "only the alternative
    characteristics are evaluated to see if it can be cast", and CR 601.3e is
    the general rule both instance - those characteristics "replace the
    object's characteristics for this determination". So the named face is
    what decides the cost paid (`Casting.base_cost` reads that face's own
    `mana_cost`, not the combined `{1}{R} // {W}` the printing carries, CR
    709.4b), what the spell may target (CR 709.3b/715.3b: on the stack it has
    only that face's characteristics) - Tear takes an enchantment where Wear
    takes an artifact - and, for an Adventure, where the card goes as it
    resolves (CR 715.3d exiles it instead of putting it into a graveyard).

    Two refusals, both in the engine's own words, neither of them a silent
    fallback to the front half:

    - a face this card does not print ("Wear has no half called Naturalize");
    - a face named on a card that has no halves to choose between
      ("Lightning Bolt is not a split card (CR 709)").

    An Adventure face used to be a third: the api refused it with a reason of
    its own while the engine's `Casting.targeting` read the whole printing for
    every layout but a split card, which measurably announced Stomp with no
    target requirement at all. Engine commit fd28021 reads the chosen face on
    every casting path, so it is cast rather than refused.

    Casting *both* halves of a split card with fuse (CR 702.102) is a different
    announcement again (`Casting.cast_fused`), and this api does not offer it
    yet: this field always names exactly one face."""
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
    """The keyword this cast is announced under (CR 601.2b).

    Two different jobs, one field. For a card that is **not** in `player`'s
    hand it is the CR 601.3 permission ("a player can begin to cast a spell
    only if a rule or effect allows that player to cast it") that makes
    casting from that zone legal at all. For a card that **is** in hand,
    CR 402.3 already permits the cast and the keyword instead names the
    CR 118.9 alternative cost paid rather than the printed mana cost - evoke
    (CR 702.74a), dash (CR 702.109a), morph (CR 702.37a), megamorph
    (CR 702.37b), disguise (CR 702.168a), bestow (CR 702.103a) or warp's hand
    half (CR 702.185a). `alternative_cost` below takes exactly the same
    announcement by index or name; only one may be applied (CR 118.9a).

    Every one of these is read straight off the engine's own
    `machines/casting.py::keyword_cast_options`/`marker_cast_options`/
    `granted_cast_options` - see `serialize.py`'s `cast_options` field, now on
    a hand card view as well as a graveyard/exile one (and
    `available_actions`'s `cast`-kind entries, each naming its own `keyword`)
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
    - `"Evoke"`, `"Dash"`, `"Morph"`, `"Megamorph"`, `"Disguise"`,
      `"Bestow"`, `"Warp"` - a card in HAND, cast for that keyword's cost
      rather than its printed mana cost (CR 702.74a, 702.109a, 702.37a,
      702.37b, 702.168a, 702.103a, 702.185a).
    - a printed CR 604.6/CR 113.6f sentence ("You may cast this card from
      your graveyard using its X ability") opening one of the above keywords
      in a zone it does not otherwise reach - `keyword` is still that
      keyword's own name, only the zone changed.
    - any keyword another permanent's static ability is granting this spell
      right now (CR 610.5's "... spells you cast ... gain X as you cast
      them", or CR 611.3d's "each ... card in your graveyard has X") - named
      the same way a printed one would be.

    A card covered by a live standing `PlayPermission` (Past in Flames'
    graveyard grant, a flashback grant, a card exiled and made playable
    "this turn") needs no `keyword`: that permission is found and used
    automatically whenever one is live for this player and card - see
    `play_permissions` on the state view. Cascade's hit and a suspended
    card's own free cast (CR 702.62a's third ability) are not
    `PlayPermission`s any more (battle 8): accepting either casts the card
    immediately while the permitting spell or ability is still resolving,
    answered through `decide`, never through a later `cast`."""
    convoke: list[ConvokeChoice] | None = None
    """Creatures tapped for convoke (CR 702.51a). Applied after the total
    cost is determined, never as part of the announcement."""
    delve: list[UUID] | None = None
    """Cards exiled from the payer's graveyard for delve (CR 702.66a), one
    per generic mana in the total cost."""
    improvise: list[UUID] | None = None
    """Untapped artifacts you control, tapped for improvise (CR 702.126a), one
    per generic mana in the total cost. Like convoke and delve it is applied
    after the total cost is determined (CR 702.126b), never as part of the
    announcement - `object_costs.improvise.candidates` on `GET
    .../affordances/{card_id}` lists the artifacts that qualify, and a cast
    row names a set that pays in its own `improvise`."""
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
    cards pitched to Force of Negation, the Mountains sacrificed to Fireblast,
    the white card Solitude's evoke exiles (CR 702.74a).
    `alternative_costs[i].requires` says how many are needed, and that same
    view's `candidates` says which cards actually qualify.

    Read by *both* announcements, not just one: `alternative_cost` (the option
    by index or name) and `keyword` (the same option named by its keyword) are
    two spellings of one CR 601.2b announcement, so a seat that announced
    evoke the way `legal-actions` and a hand card's `cast_options` spell it -
    by keyword - names its pitch here exactly as one announcing by index
    would. Handing this to only one of the two branches is what made
    `keyword="Evoke"` unusable for every non-mana evoke cost."""
    sacrificing: list[UUID] | None = None
    """The permanents chosen to pay a printed "As an additional cost to cast
    this spell, sacrifice ..." sentence (CR 118.8a) - Fling's own creature.

    An *additional* cost, not an alternative one, so it stacks with the
    printed mana cost rather than replacing it, and it is announced at
    CR 601.2b and folded into the total cost at CR 601.2f
    (`Casting._charge_printed_additional_costs`). The engine has carried
    `Casting.sacrificing`/`.discarding`/`.exiling`/`.tapping` all along with
    nothing on this request able to fill them, so Fling was announced for
    {1}{R}, sacrificed nothing, and refused to be paid.

    Same vocabulary and same shape as `ActivateRequest.sacrificing` on
    purpose: the engine decides how many objects a given sentence asks for and
    whether each one qualifies, and this only carries the caster's answer.
    Naming too few is refused with the engine's own count ("sacrifice needs N
    card(s) chosen, M given"), never silently cast for less."""
    discarding: list[UUID] | None = None
    """Same shape, for a printed "discard ..." additional cost - Cathartic
    Reunion's two cards (CR 701.9a)."""
    exiling: list[UUID] | None = None
    """Same shape, for a printed "exile ..." additional cost - Abhorrent
    Oculus's six graveyard cards (CR 406.2; the component's own `zone` says
    where each card must presently be)."""
    tapping: list[UUID] | None = None
    """Same shape, for a printed "tap ..." additional cost - Swallow Whole's
    "tap an untapped creature you control"."""
    optional_costs: list[OptionalCostChoice] | None = None
    """The optional additional costs this cast pays (CR 118.8, announced at
    CR 601.2b: "If the spell has alternative or additional costs that will be
    paid as it's being cast such as buyback or kicker costs ..., the player
    announces their intentions to pay any or all of those costs").

    One entry per cost, named by its keyword - the `keyword` of an entry in
    the cast row's own `optional_costs` (`GET .../legal-actions`), handed back
    as read: `[{"keyword": "Kicker"}]` kicks Burst Lightning (CR 702.33a), and
    CR 702.33d is what its "if this spell was kicked" reads afterwards.
    Absent or empty pays none of them, which is every cast written before this
    field existed.

    Refused in the engine's own words: a keyword the card does not have
    ("Lightning Bolt has no kicker ability (CR 702.33a)"), plain kicker paid
    twice, and a kicked total the sources cannot pay."""
    dry_run: bool = False
    reasoning: str | None = None
    """T10: this cast's own reasoning - required for an agent seat
    (`records.require_reasoning`), optional for a human one."""


class OptionalCostChoice(RequestModel):
    """One optional cost a cast pays (`CastRequest.optional_costs`).

    `keyword` is the cost's own keyword, case-insensitively: `Kicker` and
    `Multikicker` (CR 702.33a/c, `Casting.pay_kicker`), `Replicate`
    (CR 702.56a, `Casting.pay_replicate`), `Offspring` (CR 702.175a,
    `Casting.pay_offspring`), `Blight` (CR 701.68a's "you may blight N",
    `Casting.pay_blight`), `Gift` (CR 702.174a, `Casting.pay_gift`),
    `Entwine` (CR 702.42a, `Casting.choose_modes(entwined=True)` - name every
    mode in `modes` too), `Escalate` (CR 702.120a, `Casting.pay_escalate` -
    charged for every mode beyond the first whether or not it is named; name
    it to hand over the objects a non-mana escalate cost takes) and
    `Harmonize` (CR 702.180a/b, `Casting.pay_harmonize` - the creature a
    `keyword: Harmonize` cast taps to reduce its cost) - every optional cost
    choice the engine charges."""

    keyword: str
    times: int = 1
    """How many times it is paid. Only a repeatable cost (multikicker,
    replicate) takes more than 1; the engine refuses a second payment of any
    other."""
    choosing: list[UUID] | None = None
    """The objects the cost names: a kicker or replicate cost that is not only
    mana, the one creature a blight cost puts its -1/-1 counters on, the cards
    a non-mana escalate cost takes (one set per mode beyond the first), and the
    at most one creature harmonize taps."""
    opponent: str | None = None
    """The opponent a gift is promised to (CR 702.174a)."""


class ConvokeChoice(RequestModel):
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


class CrewRequest(BoardRead):
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


class EquipRequest(BoardRead):
    """CR 702.6a: "Equip [cost]" means "[Cost]: Attach this permanent to
    target creature you control. Activate only as a sorcery."

    A keyword ability the engine builds on its own (`machines.keywords.
    equip_activation`) rather than one of the Equipment's
    `activated_abilities`, so there is no ability index to name - the same
    reason crew and cycling have their own request shapes."""

    player: str
    card_id: UUID
    """The Equipment."""
    target: UUID
    """The creature to attach it to - CR 702.6a's "target creature you control"."""
    mana_sources: list[UUID | ManaSource] | None = None
    dry_run: bool = False
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


class CycleRequest(BoardRead):
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


class SpecialActionRequest(BoardRead):
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


class TargetRequest(RequestModel):
    source_id: UUID
    """A spell on the stack, a card in hand, or a permanent."""
    ability_index: int | None = None
    """For an activated ability on a permanent."""
    mode_index: int | None = None
    target: str
    """A card id or a player name."""


class ActivateRequest(BoardRead):
    # The ONE request shape exempt from `RequestModel`'s `extra="forbid"`,
    # and the exemption is about `shares`.
    #
    # `ui/actions.js`'s `bodyFor` sends `shares` for any targeting action
    # whose affordance carries a division, an activated ability included
    # (`tests/test_ui_actions.py::
    # test_an_activated_abilitys_division_sends_shares_too` asserts it does),
    # and this shape has never declared the field - so pydantic has always
    # dropped it and `engine_api.activate_ability` has never seen one. Making
    # that a 422 would turn a silent wrong answer into a human player unable
    # to activate a divided ability at all, mid-game, and honouring it needs
    # division support on the activation path in `magic-cards-model` (the
    # engine's `announce_division` lives on `Casting`, i.e. the cast path
    # only) - a change in the other repo, not this one.
    #
    # So: accepted and dropped, exactly as before, with a note rather than a
    # pretence. This costs nothing here, because the advisor gate no longer
    # reads the body at all - it reads this route's own declared shape
    # (`game_access.DRY_RUN_ROUTES`), and `dry_run` below is a real field, so
    # a probe on this route is the permitted case and any other extra key is
    # inert.
    model_config = ConfigDict(extra="ignore")

    player: str
    card_id: UUID
    ability_index: int = 0
    targets: list[str] | None = None
    mana_sources: list[UUID | ManaSource] | None = None
    x_value: int | None = None
    """CR 107.3a's X, announced as this activation begins (CR 602.2b) - the
    activated-ability twin of `CastRequest.x_value`. Fiend Artisan's
    `{X}{B/G}, {T}, Sacrifice another creature: Search ...` is the pool's
    example: omitting this for an ability whose cost carries `{X}` is refused
    with the engine's own `needs_x` answer (`affordances.ability_hints`,
    `AbilityView.needs_x`) rather than silently activated for X=0."""
    sacrificing: list[UUID] | None = None
    """The objects an activation cost's own `[Sacrifice|Discard|Exile] a
    ...` piece needs named (CR 118.3) - Goblin Bombardment's own "Sacrifice
    a creature", read the same general way `CastRequest.choosing` already
    reads Fireblast's Mountains: the engine (`Game.activation_cost`,
    `machines.cost.ComponentKind`) decides how many a given ability's cost
    actually asks for and whether each object named qualifies, this field
    only carries the seat's answer. A list, not a single id, because a cost
    can name a quantity greater than one ("Sacrifice two creatures") - the
    engine consumes as many as the cost needs and refuses with its own count
    if too few were named ("sacrifice needs N card(s) chosen, M given")."""
    discarding: list[UUID] | None = None
    """Same shape as `sacrificing`, for an activation cost's own `Discard
    ...` piece - Waker of Waves' `{1}{U}, Discard this card: ...` is the one
    already reachable through `off_zone_activation`; a battlefield ability
    with an independent discard cost reaches it here."""
    exiling: list[UUID] | None = None
    """Same shape again, for an activation cost's own `Exile ...` piece -
    Back from the Brink's "Exile a creature card from your graveyard and pay
    its mana cost" is the engine's own example (`Game.activation_cost`'s
    docstring). Only a *battlefield* activation reaches this: the
    off-battlefield path (`machines.keywords.off_zone_activation`) has no
    exile-as-cost shape yet, and naming this for one is refused rather than
    silently dropped (`engine_api._activate_off_zone_ability`)."""
    color: str | None = None
    """Color letter ("W", "U", "B", "R", "G", "C") chosen for a mana ability
    that offers one - an "any color" source or a dual land. Ignored by any
    ability that does not need a choice."""
    dry_run: bool = False
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


class AttackPair(RequestModel):
    card_id: UUID
    defender: str
    """A player name or a planeswalker card id."""


class AttackRequest(BoardRead):
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


class PlayLandRequest(BoardRead):
    player: str
    card_id: UUID | None = None
    card: str | None = None
    dry_run: bool = False
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


class PassPriorityRequest(BoardRead):
    player: str
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


class ConcedeRequest(BoardRead):
    """CR 104.3a: a player may concede at any time, and leaves the game
    immediately. No priority is required and nothing can refuse it, so unlike
    every other action request this one has no `dry_run` - there is nothing to
    ask permission for."""

    player: str
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


class AdvanceRequest(BoardRead):
    """Who is advancing the turn - required, unlike `ActorRequest`, because
    `Game.advance` now authorizes the call against the active player (or
    their controller, CR 723.5) rather than merely logging who asked."""

    player: str
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


class BlockPair(RequestModel):
    blocker: UUID
    """The creature declaring the block."""
    blocked: UUID
    """The attacking creature it blocks."""


class DeclareBlockersRequest(BoardRead):
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


class DeclaredPairRequest(RequestModel):
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


class DamageAssignmentRequest(RequestModel):
    """One `(target, amount)` entry for the `assign_combat_damage` kind's
    `decide` answer (CR 510.1c, CR 702.19b) - the request-side twin of the
    engine's `machines.decisions.DamageAssignment`.

    `target` is a card id (a blocker, from `pending_decision.cards[1:]`) or a
    player's username (the defending player or planeswalker, offered only
    once every blocker has been assigned its own `pending_decision
    .lethal_damage` and only when `pending_decision.trample` is true) - the
    same "card id or player name" token shape `DeclaredPairRequest.defender`
    already uses, told apart the same way (`decisions.damage_answer`).
    """

    target: str
    amount: int


class DecideRequest(BoardRead):
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
    mana_sources: list[str] | None = None
    """The `pay_mana` kind's optional companion to `accept` (CR 608.2g, CR
    605.3a; engine PR #17, B6-08): which of `pending_decision.mana_sources` -
    the payer's own untapped permanents - to activate while paying, rather
    than leaving the engine to tap them all. Legal only alongside `accept:
    true` - naming sources while declining is refused, matching the engine's
    own `Answer` shape. `None` (the default) is the plain yes/no this kind has
    always taken; an empty list is a real, different answer from omitting the
    field - pay from the floating pool alone, activating none of the offered
    sources."""
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
    goes back on top. Empty is a real answer: "keep everything on top".
    How many may be named is `pending_decision.exact`/`.amount`, the same
    count `choose_cards` is held to: `exact=True` means exactly `amount`
    (Codecracker Hound's "put one into your hand and the other into your
    graveyard"), `exact=False` means zero up to `amount` (CR 107.1c) - a
    plain scry/surveil's `amount` is every card looked at, while Devourer of
    Destiny's "may put one of those cards back" is `amount=1`, a real
    ceiling rather than "any"."""
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
    damage: list[DamageAssignmentRequest] = Field(default_factory=list)
    """The `assign_combat_damage` kind's answer (CR 510.1c, CR 702.19b) - how
    one attacking creature's combat damage divides among its blockers and,
    with trample, the player or planeswalker it attacks. Every entry's
    `amount` must sum to exactly `pending_decision.amount` (the attacker's
    power), and damage may reach the attacked player or planeswalker only
    once every blocker named in `pending_decision.cards[1:]` has been
    assigned at least its own `pending_decision.lethal_damage` (CR 702.19b) -
    see `DamageAssignmentRequest`."""
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


class MulliganRequest(BoardRead):
    """One seat's CR 103.5 declaration: "a player who is dissatisfied with
    their initial hand may take a mulligan".

    `BoardRead` because this is a mutating move a seat makes and records
    (`mulligan_declared`, one of `scripts/eval_report.py`'s `PLAY_ACTIONS`),
    so a refusal of it belongs in the same measured split as every other:
    without an `expected_seq` to name, a seat's first declaration of the game
    was the one move whose refusals could not be told apart from a lost race.
    Optional here exactly as it is everywhere else - the browser names no
    board and is refused on nothing new."""

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


class OpeningHandActionRequest(RequestModel):
    """One CR 103.6 action with a card in this seat's own opening hand -
    "some cards allow a player to take actions with them from their opening
    hand"."""

    player: str
    card_id: UUID
    reasoning: str | None = None
    """T10: required for an agent seat (`records.require_reasoning`),
    optional for a human one."""


class PregameSettleRequest(BoardRead):
    """Admin-only: run the whole pregame (CR 103.5's mulligan process, CR
    103.6's opening-hand actions) forward, all at once, over every seat.

    `BoardRead` for the same reason every other mutating request carries it:
    this is a command like any other (`commands._run_pregame_settle`), it
    reaches the same `records.legal_actions_before` sites, and an operator
    settling a pregame against a board that has since moved is owed the news
    that it moved rather than a settled game they did not ask for."""

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
    restriction: RestrictionView | None = None
    """The CR 106.6 restriction the mana this ability makes would carry
    (`machines.cost.spend_restriction` over the ability's own effect text),
    or `None` for an ability that prints no such sentence.

    The read-only half of `ManaPoolView.restricted`, one step earlier: a seat
    deciding *which* ability to tap a two-ability source for needs to know what
    the mana would be tied to before it taps, not after."""
    mana_amount: int | None = None
    """How much mana this mana ability adds if activated now, or `None` for an
    ability that is not a mana ability.

    Board-resolved (CR 605.1a's "add X instead" clauses: Urza's Tower beside
    a Mine and a Power-Plant adds 3), and 0 when it cannot be activated now
    (CR 602.5, CR 302.6's summoning sickness) or its mana type is undefined
    (CR 106.5). A restricted ability (`restriction`) carries its full amount:
    CR 106.6 restricts how the mana is spent, not whether it is made."""
    mana_cost_value: int | None = None
    """The mana value of the mana part of this mana ability's own activation
    cost - 1 for a `{1}, {T}` or `{W/U}, {T}` filter, 0 for a bare `{T}` - or
    `None` for an ability that is not a mana ability. Read with `mana_amount`,
    it is what a filter adds net of the mana it consumes."""


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
    """One CR 601.2b keyword a hand, graveyard or exile card can be cast with
    right now (`serialize._cast_option_view`).

    From a graveyard or exile it is a CR 601.3 permission to cast the card
    from where it sits at all - flashback, mayhem, plot, foretell, madness,
    airbend, a printed CR 604.6 zone permission, or a keyword granted by
    another permanent's static ability. From the hand it is a bare CR 118.9
    alternative cost that replaces the printed mana cost, which is evoke,
    dash, morph, megamorph, disguise, bestow and warp's hand half.

    `usable_now` is the option's own answer (`KeywordCastOption.why_not`:
    zone, marker owner and turn, "discarded this turn", a keyword's own "as a
    sorcery" clause) and never CR 117.1a's timing rule for the card itself -
    `GET .../legal-actions` is what answers "could I do this right now"."""

    keyword: str
    rule: str
    pays_mana_cost: bool
    cost: str | None
    usable_now: bool
    why_not: str | None
    granted_by: str | None = None


class CardFaceView(BaseModel):
    """One face of a double-faced card (`serialize._faces_view`), as printed."""

    name: str
    mana_cost: str | None = None
    type_line: str | None = None
    oracle_text: str | None = None


class _CardViewFields(BaseModel):
    """Every field a card view can carry. `CardView` requires the `id`;
    `ContentsCardView` is the same view that never names one."""

    id: str | None = None
    name: str
    card_name: str | None = None
    zone: str
    controller: str | None = None
    owner: str | None = None
    tapped: bool
    face_up: bool
    type_line: str | None = None
    mana_cost: str | None = None
    mana_value: float | None = None
    """This object's mana value (CR 202.3) - set only on the stack, where
    CR 202.3e makes it depend on the chosen X (`x_value` below), which is why
    it is not simply carried for every zone: off the stack, `mana_cost` plus
    a client's own symbol counting already gets CR 107.3g's "X is 0" answer
    for free. See `serialize._mana_value`."""
    x_value: int | None = None
    """The X a spell with `{X}` in its mana cost was cast for (CR 107.3a),
    set only while it sits on the stack. Absent for a spell with no `{X}`,
    not only a zero one - `mana_cost` already says whether one was ever
    announced."""
    oracle_text: str | None = None
    """The printed rules text. For a double-faced card, the face that is up:
    the front face anywhere but the battlefield or stack (CR 712.8a)."""
    faces: list[CardFaceView] | None = None
    """Double-faced cards only (CR 712): every face, front first."""
    counters: dict[str, int] | None = None
    damage: int | None = None
    summoning_sick: bool | None = None
    power: int | None = None
    toughness: int | None = None
    colors: list[str] | None = None
    """Color letters as the engine computes them right now
    (`serialize._current_colors`)."""
    mana_value: float | None = None
    """The engine's `CardState.mana_value`."""
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


class CardView(_CardViewFields):
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


class RestrictionView(BaseModel):
    """A CR 106.6 "Spend this mana only to ..." sentence, as a seat sees it.

    `text` is the printed sentence verbatim, so a refusal quotes the card
    rather than the parser's reading of it.

    `understood` is whether the engine turned that sentence into anything it
    can check. **False is not cosmetic and is never hidden**: an unread
    restriction is fail-closed in the engine - the mana exists, is announced
    (CR 106.4a), empties with the pool, and pays for nothing at all - so a view
    that showed the mana without this flag would be telling the player they
    have a resource they cannot spend.

    Deliberately two fields and no third: the engine's own `SpendClause` tuple
    is the parser's internal shape and is still being changed underneath, so
    publishing it here would pin it.
    """

    text: str
    understood: bool


class RestrictedManaView(BaseModel):
    """How much of a pool carries one particular CR 106.6 restriction
    (`models.mana.RestrictedMana`).

    `amounts` is a **subset** of `ManaPoolView.floating`, never a replacement
    for it: CR 106.4a has the player announce all the mana that is there, and
    the restriction narrows only what may be done with it. Without the amount
    two restricted `{C}` and two plain `{C}` are one `floating {"C": 2}` and
    nothing tells them apart.
    """

    restriction: RestrictionView
    amounts: dict[str, int]


class SpendEffectManaView(BaseModel):
    """`RestrictedManaView`'s twin for CR 106.6's *other* clause - "have an
    additional effect that affects the spell or ability that mana is spent on"
    (`models.mana.SpendEffectMana`).

    Its own list rather than a permissive restriction, because it narrows
    nothing: the mana pays for whatever it could have paid for anyway, and the
    sentence only says what happens to the spell it is spent on. So there is no
    `understood` half to report - an unread one costs the player nothing.
    """

    text: str
    amounts: dict[str, int]


class ManaPoolView(BaseModel):
    floating: dict[str, int]
    snow: dict[str, int]
    restricted: list[RestrictedManaView]
    """The CR 106.6-restricted part of `floating`, one entry per distinct
    restriction. Empty for a pool with nothing tagged - always present, so a
    client never has to tell "none" from "this build does not report it"."""
    spend_effects: list[SpendEffectManaView]
    """The part of `floating` carrying CR 106.6's additional-effect clause,
    one entry per distinct effect. Same always-present shape as `restricted`."""


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
    lands_played: int | None = None
    """Lands this player has played this turn (`Game.lands_played`). Optional
    only so a snapshot recorded before the field existed still reads."""
    land_allowance: int | None = None
    """How many lands this player may play this turn (`Game.land_allowance`)."""


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
    source_card_id: str | None = None
    """The id of the object asking (`decisions.PendingDecision
    .source_card_id`): the card or token, or the card behind an ability on
    the stack. `None` for a question no object asks, and for a viewer who
    cannot see the zone that object is in (`visibility
    .redact_pending_decision`)."""
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
    lethal_damage: list[int] | None = None
    """`assign_combat_damage` only (CR 702.19b): the damage each of `targets`
    still needs before it counts as assigned lethal damage, aligned with it
    positionally - 0 for the defending player or planeswalker itself. `None`
    for every other kind (`decisions.PendingDecision.lethal_damage`)."""
    trample: bool | None = None
    """`assign_combat_damage` only: whether the attacker has trample (CR
    702.19b), so damage may reach the defending player or planeswalker once
    every blocker has its own `lethal_damage`. `None` for every other kind."""
    mana_sources: list[str] | None = None
    """`pay_mana` only: the untapped permanents whose mana abilities the
    payer may activate while paying (CR 608.2g, CR 605.3a; engine PR #17,
    B6-08) - a subset of these is what `decide`'s own `mana_sources` names
    back. Empty for every other kind."""
    card_details: list[CardView] | None = None
    """Only for the deciding player - the full view of exactly the ids
    `cards` names, resolved from zones `redact_view` otherwise strips
    entirely (`visibility.redact_pending_decision`)."""
    answer_field: str | None = None
    """Which single MCP tool parameter carries this kind's answer
    (`decisions.TOOL_FIELD_FOR_KIND`) - `accept` for a `may`, `card_ids` for
    a `choose_cards`, `choice` for a mode, `moved` for a scry."""
    answer_with: str | None = None
    """The call that answers this exact question, paste-ready, naming this
    decision's own `generation` (`decisions.PendingDecision.answer_with`).

    Emitted rather than derived because the mapping from `kind` to parameter
    disagrees with the engine-facing field name on ten of the nineteen
    kinds, and lived only in the MCP `decide` tool's own docstring before
    this - see `decisions.TOOL_FIELD_FOR_KIND`'s note for the table of
    divergences and the refusal it was costing."""


class CombatAttackView(BaseModel):
    attacker: str
    defender: str
    blockers: list[str]


class CombatView(BaseModel):
    attacks: list[CombatAttackView]


class KnownLibraryCardView(BaseModel):
    """One library card a player knows, with where it sits right now
    (`serialize._known_cards_view`; CR 701.20d ends it on a reordering)."""

    index_from_top: int
    source: str | None = None
    """The name of what showed the card (`KnownCard.source`), when recorded."""
    card: CardView


class KnownCardsView(BaseModel):
    """What one player knows about hidden cards (`Game.known_to`): cards in
    another player's hand, library cards with their live position, and
    sideboard cards. The player's own hand and sideboard are never listed."""

    hand: list[CardView]
    library: list[KnownLibraryCardView]
    sideboard: list[CardView]


class ContentsCardView(_CardViewFields):
    """A card view with no `id` (`serialize._contents_view` never sets one) -
    a contents entry names no single object, because naming one would pin it
    to a place in an unordered zone."""


class ContentsEntryView(BaseModel):
    """One entry of `library_contents`/`sideboard_contents`: identical cards
    counted together, with no id and no position (CR 401.2)."""

    card: ContentsCardView
    count: int


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
    known_cards: dict[str, KnownCardsView] | None = None
    """Keyed by the player who knows. `visibility.redact_view` keeps only the
    viewer's own entry (the admin sees all, a viewer who is nobody gets
    `{}`)."""
    library_contents: dict[str, list[ContentsEntryView]] | None = None
    """Only with `?include=contents`: each player's own library as an
    unordered multiset, cut to the viewer's own entry by redaction."""
    sideboard_contents: dict[str, list[ContentsEntryView]] | None = None
    """Only with `?include=contents`: the sideboard, shaped like
    `library_contents`."""
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
    """`PUT .../step`'s response - the harness teleport, always just the
    board: it moves the raw turn marker (`engine_api.set_step`,
    `Game.advance_to`) rather than running `Game.advance`'s own
    beginning-of-step turn-based actions and triggers, so there is nothing
    beyond `GameStateView` for it to carry."""


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

    `notices` is the one `Outcome.detail` key declared here rather than left
    to `extra="allow"`: unlike `cost`/`tapped`/`spell_id`/..., it is not
    per-action - `commands.run_command` files it onto every command kind's
    outcome generically (`commands._attach_notices`), so every route this
    model serves can carry one. Each entry is one of `Game`'s 19 fail-closed
    notice records (`{"kind": ..., **notice.model_dump(mode="json")}`, plus
    this api's own `zone`/`controller` bookkeeping - see
    `commands._new_notices`), already redacted for this response's own
    viewer (`app.audit_outcome`) - not the engine's raw list.
    """

    model_config = ConfigDict(extra="allow")

    allowed: bool | None
    reason: str | None = None
    paused: bool = False
    pending_decision: PendingDecisionView | None = None
    notices: list[dict[str, Any]] = Field(default_factory=list)
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


class MintSeatRequest(RequestModel):
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
    role: Literal["seat", "advisor"] = "seat"
    """What kind of credential this uid is: a seat that plays `player`, or an
    advisor that reads `player`'s view and may not act (`repository.Advisor`,
    enforced by `game_access.py`). Defaulted to `"seat"` so a caller written
    before advisors existed reads exactly what it always read; a client that
    holds uids without a game pin - one mcp process serving several games -
    needs it to know which of the two it is driving."""


class GameSummary(BaseModel):
    game_id: str
    players: list[str]
    step: str | None = None
    unloadable: str | None = None
    """Set only when this game's row exists but this build cannot decode the
    snapshot in it - the decoder's own message, verbatim
    (`repository.GameUnloadable.reason`), which for the case this field
    exists for is the engine's `SchemaVersionMismatch` after
    `Game.CURRENT_SCHEMA_VERSION` moved past what the row was written at.
    `None` for every game that loaded normally.

    Such a game is *listed*, not omitted: a game somebody played must not
    read as a game that vanished. `players` still comes back (from the
    seats/decklists columns, which need no decoding); `step`, which can only
    come from the board, is `None`. Opening the game directly answers 409
    with this same text."""


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
    unloadable: str | None = None
    """`GameSummary.unloadable` on this row shape - see there. On such a row
    every scalar that could only be read off the board is at its "unknown"
    value: `turn_number`, `active_player` and `phase` are `None`, and
    `is_over` is `False`, since a board this build cannot read is a board on
    which CR 104.1 cannot be judged and must not be guessed at."""


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


class ReviewAdvice(BaseModel):
    """One `audit.ADVICE_ACTION` row - the mtg-buddy advisor's own proposal
    to a human seat - paired with whether it was approved and what the seat
    did about it (`review.py`'s `_advice`).

    A stream of its own, beside `declarations` rather than folded into it: a
    declaration's window runs from one `plan_declared` row to the next *by
    any seat*; an advice row is per-decision-point and would fragment that
    window if it were treated as a second kind of marker. It is not - see
    `_advice`'s own docstring.

    Nothing here grades the advice either, for the same reason
    `ReviewDeclaration` grades no prediction: whether it was *good* is a
    judgement, not an outcome. What is reported is one of four things:
    approved and followed; approved, attempted, and refused by the engine
    (`did` is `None` - a refusal is a failed attempt, never the outcome -
    but `refused` carries the row and the engine's own verbatim reason, so
    nothing is lost); never approved and the seat did something else (`did`
    is that row); or never approved and the seat did nothing before the next
    piece of advice (`did` is `None`, `refused` is empty).

    `approved` and `followed` are independent, and the pair a reader is most
    often after is `approved: true, followed: false` - the seat agreed to
    this and then did something else. That takes BOTH fields to see: `did`
    alone cannot say it, and `approved` alone cannot either.
    """

    seq: int
    ts: str | None = None
    turn: int | None = None
    step: str | None = None
    phase: str | None = None
    player: str | None = None
    summary: str | None = None
    """Server-derived from the proposal's request body - card names resolved
    from ids, targets, cost."""
    reasoning: str | None = None
    """The advisor's own stated reason for proposing this."""
    path: str | None = None
    """The route the proposal would have posted to."""
    body: dict[str, Any] | None = None
    """The proposed request body, verbatim."""
    based_on_seq: int | None = None
    """The ledger position the proposal was made against."""
    validated: bool | None = None
    """Whether the proposal could be checked with `dry_run` before ever
    reaching the human - `False` for a request shape with no `dry_run`
    support, and therefore not probed."""
    approved: bool = False
    approved_seq: int | None = None
    """The matching `advice_approved` row's own `seq` - paired by `seq`,
    never by comparing bodies, since a field like `mana_sources` legitimately
    differs between what was proposed and what the engine actually taps."""
    did: ReviewEntry | None = None
    """The first real move this seat made after the advice and before its
    own next piece of advice, excluding any row the engine refused. `None`
    covers two different situations distinguished only by `approved`: the
    approved attempt was refused, or nothing followed at all."""
    followed: bool = False
    """Whether `did` actually matches the proposal - same action kind for
    `path`, and the same card where `body` names one. Independent of
    `approved`: a human can do exactly what was proposed without ever
    approving it, and can approve a proposal and then do something else.
    Rendered as its own line above the `did` frame (`ui/render/review.js`'s
    `adviceFollowThroughNode`), because without it the two approved outcomes
    are the same page."""
    refused: list[ReviewEntry] = Field(default_factory=list)
    """Every row by this seat, in the same window, that the engine refused -
    carrying its verbatim `reason` inside each row's own `detail`, exactly as
    `GET .../log` shows it. `did` never covers this: a refusal is a failed
    attempt, not "what happened", but the reason it failed is not thrown
    away either - "you approved this and the engine refused it because X" is
    the single most instructive line this page can show. Ordinarily zero or
    one row; not assumed to be at most one, since nothing stops a seat
    retrying a refused attempt more than once."""


class GameReviewResponse(BaseModel):
    """`GET /api/games/{game_id}/review` - post-game only, and never open."""

    game_id: str
    players: list[str]
    created_at: str | None = None
    ended_at: str | None = None
    """When the game was ended from the lobby or deleted, `None` for one that
    simply finished."""
    is_over: bool | None = None
    """`None` only on a `reconstructed` review with no `game_over` row: with
    no decodable board there is no `Game.is_over` to read, and the absence of
    a recorded ending establishes nothing. Never guessed - see
    `unestablished`."""
    winner: str | None = None
    drawn: bool | None = None
    ended_seq: int | None = None
    """The `game_over` row's own `seq`, `None` for a game that was ended
    before it finished."""
    reconstructed: bool = False
    """True when this review was rebuilt from the ledger because this build
    cannot decode the game's stored snapshot (`review.py`). Everything a
    reader sees is then a frame recorded when it happened, not a reading off
    a final state - which is the honest description of it, and the reason it
    is said in the payload rather than left to be inferred."""
    unloadable: str | None = None
    """On a `reconstructed` review, the engine's own refusal verbatim
    (`repository.GameUnloadable.reason`); `None` otherwise."""
    unestablished: list[str] = Field(default_factory=list)
    """The names of the fields above that this review could not establish at
    all, and which are therefore `None` rather than a value. Empty for every
    review built from a decodable final state."""
    declarations: list[ReviewDeclaration] = Field(default_factory=list)
    advice: list[ReviewAdvice] = Field(default_factory=list)
    """Every piece of advice the mtg-buddy advisor made to a seat in this
    game, paired with whether it was approved and what happened next - a
    stream of its own, beside `declarations` rather than folded into it. See
    `ReviewAdvice`."""
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
    notices: list[dict[str, Any]] = Field(default_factory=list)
    """`commands.run_command`'s notices for this `advance` (see
    `ActionOutcome.notices`'s own docstring) - absent whenever there is
    nothing to report: always on the paused branch (`commands.
    _attach_notices` never runs against a `PAUSED` `RunResult`), and on a
    completed one too when nothing failed closed."""
    log_seq: int | None = None


class StateBasedActionsResponse(GameStateView):
    """`POST .../state-based-actions` - always the full state view, both
    paused and completed."""

    allowed: bool | None = None
    paused: bool = False
    destroyed: list[str] | None = None
    changed: list[str] | None = None
    notices: list[dict[str, Any]] = Field(default_factory=list)
    """See `ActionOutcome.notices`."""
    log_seq: int | None = None


class ResolveTopResponse(GameStateView):
    """`POST .../resolve-top` - always the full state view. `reason` is set
    (with `resolved: None`) when `engine_api.resolve_top` itself refused
    (no legal object to resolve), which is still a `RunStatus.COMPLETED`
    result, not a paused or a raised one."""

    resolved: str | None = None
    zone: str | None = None
    reason: str | None = None
    changed: bool | None = None
    """Whether resolving actually changed anything observable - `None` only
    when nothing was there to resolve (`resolved` is also `None` then). See
    `commands._resolution_state`; the same marker `pass_priority`'s own
    `resolve_top` outcome carries."""
    allowed: bool | None = None
    paused: bool = False
    notices: list[dict[str, Any]] = Field(default_factory=list)
    """See `ActionOutcome.notices`."""
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
    notices: list[dict[str, Any]] = Field(default_factory=list)
    """See `ActionOutcome.notices`."""
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


class TargetSlotCandidates(BaseModel):
    """The legal candidates for ONE of an announcement's targeting
    requirements (`affordances.target_candidates`), by that requirement's own
    index into `Action.target_slots` - so `slot` says which phrase these
    answer, and the phrase itself is not repeated here.

    `count` is the true total whatever the caps did to `candidates`: a seat
    weighing a sweeper needs the number even when it does not need the names,
    and a list that quietly shrank while its count shrank with it would be a
    list that lies. `truncated` and the absence of `candidates` are the two
    ways it can shrink - the per-slot cap (`affordances.CANDIDATE_SLOT_CAP`)
    and the whole answer's candidate budget (`mcp.MAX_CANDIDATE_BYTES`).
    """

    slot: int
    count: int
    candidates: list[str] | None = None
    """`"<uuid> <name> (<zone>, <controller>)"`, or `"<player> (player)"`.
    Strings rather than objects on purpose - see `affordances.candidate_line`
    for the byte measurement that decided it. Absent (never empty) when the
    answer's candidate budget was spent before this entry."""
    truncated: int | None = None
    """How many legal candidates this slot has beyond the ones listed.
    Absent, never `0`, when the list is complete."""


class ActionTargets(BaseModel):
    """What one action may legally name, per requirement slot.

    `understood: false` carries NO slots and a `note` saying why, which is
    `affordances.candidates`' existing contract kept intact: an empty
    candidate list would read as "nothing on this board is legal", a
    different and false statement, and `ui/app.js`'s `isPickingTargets`
    already branches on exactly that difference.
    """

    understood: bool
    slots: list[TargetSlotCandidates] | None = None
    more_with: str | None = None
    """Where the rest of the candidates are, present only when something was
    cut."""
    note: str | None = None
    """Why there are no candidates, present only when `understood` is false."""


class OptionalCostView(BaseModel):
    """One optional additional cost a cast row's card offers (CR 118.8) -
    `action_rows.legal_action_rows`. Its `keyword` is what
    `CastRequest.optional_costs[].keyword` takes back."""

    keyword: str
    rule: str
    cost: str | None = None
    """The cost as printed after the keyword (`{4}` for Burst Lightning's
    kicker), or `blight N`; `None` for gift, whose cost is choosing an
    opponent, and for harmonize, whose choice is the creature tapped."""
    repeatable: bool
    """Whether it may be paid more than once (multikicker, replicate) -
    `OptionalCostChoice.times`."""
    affordable: str
    """`yes`/`no`/`unknown` for this row's cost with this one added, in the
    engine's own `Affordability` words."""
    unaffordable_reason: str | None = None


class ManualManaStepView(BaseModel):
    """One mana ability to activate by hand before a cast row is payable
    (`ActionRow.activate_first`) - `POST .../actions/activate` with these
    `card_id`/`ability_index` (and `color`, when present)."""

    card_id: str
    name: str
    ability_index: int
    cost: str | None = None
    adds: str
    """The mana this activation added on a copy of the board."""
    color: str | None = None


class UnreadableModeView(BaseModel):
    """One mode of a modal spell the engine cannot read into an effect -
    `action_rows.legal_action_rows`, from the reading
    `effects.resolve_modal_spell` records a `ModeNotice` for."""

    mode_number: int
    """The mode's 1-based position on the card (CR 700.2), the number
    `CastRequest.modes` takes."""
    text: str
    notice: str


class XCostView(BaseModel):
    """What an X cast row is charged at one X its `cost` does not describe
    (`action_rows._Rows.with_charged_cost`)."""

    x: int
    cost: str | None = None
    mana_cost: str | None = None


class ActionRow(Action):
    """The engine's own `Action` as `GET .../legal-actions` serves it, plus
    what the api adds to a row (`action_rows.legal_action_rows`). Every field
    here is left off a row it does not apply to."""

    kind: ActionKind | Literal["equip"]
    """The engine's `ActionKind`, or `equip`: CR 702.6a's activation, which
    the engine's enumeration does not list and `POST .../actions/equip`
    takes (`action_rows._Rows.equip_rows`)."""

    optional_costs: list[OptionalCostView] | None = None
    """A cast row's optional costs - kicker and its siblings, entwine and
    escalate on a modal row, harmonize's creature tap on a harmonize row."""
    printed_produces: str | None = None
    """A mana row whose `produces` was resolved on this board (Urza's Tower
    making `{C}{C}{C}` beside Mine and Power-Plant) keeps the printed phrase
    here."""
    activate_first: list[ManualManaStepView] | None = None
    """A cast row the engine's auto-tap cannot pay, but which is payable once
    these mana abilities are activated by hand (CR 118.3c). Such a row reads
    `affordable: yes`; cast it after activating them, in order."""
    printed_cost: str | None = None
    """A cast row whose `cost` was changed by a cost increase, reduction or
    floor (CR 601.2f, 118.7) keeps the cost it would otherwise print here;
    `cost` is what the cast is charged. On an X row `cost` keeps the X - for
    every X not listed in `x_costs` - and when a reduction eats into the mana
    X adds, no one mana cost says that: `cost` then states the reduction in
    words (`{X}{R} less {1}`) and `mana_cost` is left off."""
    x_costs: list[XCostView] | None = None
    """An X cast row's charge at each X its `cost` does not describe, lowest
    first - a floor or a reduction reads the cost X has already left
    (CR 601.2f), so Walking Ballista under Trinisphere is `{3}` at X=0 and
    X=1 and `{X}{X}` from there on."""
    unreadable_modes: list[UnreadableModeView] | None = None
    """A modal cast row's modes the engine cannot read (CR 700.2a): chosen,
    each would resolve doing nothing, so a cast naming one is refused with
    the same `notice`."""
    improvise: list[str] | None = None
    """A cast row whose cost is payable once these untapped artifacts are
    tapped for improvise (CR 702.126a) and the rest is paid from mana. Such a
    row reads `affordable: yes`; pass these ids back as
    `CastRequest.improvise`."""


class ActionWithTargets(ActionRow):
    """The engine's own `Action`, plus the candidates the api enumerates for
    it (`app._attach_target_candidates`). A subclass rather than a re-shape:
    every field of `Action` is passed through untouched and by inheritance,
    so a field the engine adds arrives here without this file changing.

    `targets` is **required**, which is what makes this a distinguishable
    branch of `LegalActionsResponse.actions` rather than a replacement for
    `Action` - see there.
    """

    targets: ActionTargets


class LegalActionsResponse(BaseModel):
    """`GET .../legal-actions` (T11) - the engine's own `Action` model,
    reused as-is rather than re-shaped: `get_legal_actions` returns exactly
    `[action.model_dump(mode="json") for action in actions]`, and
    `records.LegalActionsBefore.actions` (surfaced redacted, per-viewer, as
    `LogEntry.legal_actions_before`) is the identical dump - now with the
    legal candidates for each targeting action folded in alongside.

    **Both names stay in `#/components/schemas`, and the union is why.**
    Typing this `list[ActionWithTargets]` alone read as an enrichment and was
    one: every `Action` field is still there, by inheritance. But FastAPI
    names a component only where a route references it, and the subclass was
    the only thing referenced - so the engine's `Action`, which downstream
    repos validate against **by name** (`tests/test_openapi_drift.py`'s own
    docstring), silently stopped being published at all. The union restores
    it without touching the wire, because it is also the truth about the
    answer: `candidates=false` (the default, and what `ui/app.js` asks for)
    answers plain engine `Action` rows, and `candidates=true` adds `targets`
    to the targeting ones - `_attach_target_candidates` sets that key or
    leaves it off, never sets it to null. `targets` being required on the
    subclass is what keeps the two branches apart: a row carrying candidates
    matches `ActionWithTargets` and only it, a row without matches `Action`
    and only it, so neither branch is decoration and a client can tell which
    it is holding by the one key that differs.
    """

    actions: list[ActionWithTargets | ActionRow | Action]
    """`ActionRow` is the same engine `Action` with the api's own additions
    (`optional_costs`, `printed_produces`, `activate_first`, `improvise`), each present only
    on a row it applies to - so a row carrying none of them is exactly an
    engine `Action`, and `Action` stays a published name."""

    last_seq: int = 0
    """The ledger position this enumeration was computed against
    (`audit.last_seq`) - the number a seat names back as
    `BoardRead.expected_seq` on whatever it decides to do about it.

    Here rather than in a second call, because a seat that has to *ask* for
    the position it just read at has already lost the thing worth naming: one
    more round trip is one more window for the board to move, and in a
    parallel gauntlet it is also one more read through the single `serve`
    process every game shares. This is the same number `GET .../log` answers
    as its own `last_seq` and the same one `seatkit.Seat.last_seq` carries
    from a `recent_log`; a seat driven by `api_client.SeatApiClient` has no
    log read in its loop at all (three calls, deliberately - see there), so
    without this it could name no board and every refusal it collected was
    unsplittable (`scripts/eval_report.py`'s `unknown` bucket).

    `0` for a game with no ledger, which is the same "name nothing" answer
    `require_current_board` already short-circuits on."""


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
    candidates: list[ObjectCostCandidateView] = Field(default_factory=list)
    """Which of the caster's cards could actually be named in
    `CastRequest.choosing` for this option, each carrying the `pays` kind of
    cost component it would answer ("exile", "sacrifice", "discard", "tap").

    `requires` says how many; this says which, and without it neither a UI's
    pitch tray nor an agent seat can tell a legal answer from a refusal:
    Solitude's evoke cost is "Exile a white card from your hand", and a hand
    of six with two white cards has four cards in it that will be refused.
    `object_costs` has published `candidates` for convoke, delve and crew from
    the start (`ObjectCostsView`); this is the same answer on the other half
    of the cost model."""
    available: bool
    unavailable: str | None = None


class ObjectCostCandidateView(BaseModel):
    """One object offered against a cost that is paid with objects rather
    than mana - convoke/delve/crew (`affordances.object_costs`'s `eligible()`)
    or an alternative cost's own `choosing`
    (`affordances._alternative_cost_candidates`). `CandidateView`'s shape
    minus `requirements` (this is a payment offer, not a targeting slot) plus
    an optional `power`, present only for a battlefield candidate, and an
    optional `pays`, present only on an alternative cost's candidates."""

    id: str
    kind: str
    name: str
    zone: str | None = None
    controller: str | None = None
    power: int | None = None
    pays: str | None = None
    """Which kind of cost component this card would answer - "exile",
    "sacrifice", "discard" or "tap" (`machines.cost.ComponentKind`). An
    alternative cost can carry more than one component ("sacrifice a creature
    and exile a card"), and a caller passing everything in one `choosing` list
    needs to know which of its cards covered which half."""


class ConvokeCostView(BaseModel):
    rule: str
    candidates: list[ObjectCostCandidateView]


class DelveCostView(BaseModel):
    rule: str
    candidates: list[ObjectCostCandidateView]


class ImproviseCostView(BaseModel):
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
    improvise: ImproviseCostView | None = None
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
