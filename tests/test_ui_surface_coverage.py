"""The drift guard for *fields*, mirroring `test_ui_decision_coverage.py`
(decision kinds) and `test_notices.py` (notice lists).

Those two guard the two surfaces that had already drifted. This one guards
the third and largest, which had no guard at all: the individual **fields**
the api publishes on the views the browser reads. Six separate live bugs -
an MDFC burned as a land, a wrath that resolved doing nothing, a spell cast
at a price the row already knew was wrong, an artifact deck that could not
be cast, a mode refused only after targeting, and kicker never offered -
survived a green suite on both sides for months for one shared reason: a
field was added to a response model, the browser was never taught to read
it, and nothing anywhere failed.

The rule this file enforces: **every field published on a browser-facing
surface is either read somewhere under `ui/`, or written down here with a
one-line reason why not.** A new field is a failing test until one of those
two things happens. That is the whole mechanism - the allowlist is not
bookkeeping, it is the thing that makes the guard survive contact with a
repo that legitimately publishes agent-facing fields the browser will never
want.

**Inheritance is in scope on purpose.** `ActionRow` extends the engine's own
`Action`, so a field the *engine* adds arrives on this api's wire without
this repo changing a line - which is exactly how `x_range`, `land_face` and
`spell_face` got published and never consumed. Fields are therefore keyed by
the class that **declares** them (`Action`, `_CardViewFields`), not by the
response model that happens to inherit them, and the failure message names
both.

Consumption detection
---------------------
There is no build step and no type information crossing the boundary (the
browser is buildless ES modules, `CLAUDE.md` says so deliberately), so this
is a static read of `ui/*.js` + `ui/render/*.js` source. The rule, in full:

1. JS comments are stripped first. A field named only in a comment is **not**
   consumed - `CardView.keywords` is mentioned in a comment in
   `ui/actions.js` and read by nothing, and without this step the guard would
   have called it wired.
2. A field counts as consumed when the stripped source contains a property
   *read* of that name: `.name` (not immediately followed by `=`), a bracket
   read `["name"]`/`['name']`/[`name`], or a destructuring binding
   `{... name ...} =` / `{... name ...})`.
3. **An assignment is not a read.** `.name =` is excluded, because in this UI
   that shape is the browser *building a request body* (`body.optional_costs
   = announced`), not reading a published field. Three fields hang on this
   distinction today - `optional_costs`, `alternative_cost`, `mana_sources` -
   and `optional_costs` is one of the six bugs, so a rule that counted the
   write would have blessed the exact defect this file exists to catch.
4. **Generic reads are declared, not guessed.** Some fields are consumed
   without their names ever appearing: `SpecialActionsView`'s `plot`,
   `foretell` and `suspend` are read as *keys* by
   `Object.keys(card.special_actions || {})` in `ui/app.js`. A by-name sweep
   flags those as dead, so they are declared in `_CONSUMED_GENERICALLY`
   together with the source snippet that does the reading - and the test
   asserts that snippet is **still in the file**, so deleting the generic
   consumer fails here rather than quietly re-arming the guard's own excuse.

Two other generic reads the sweep turned up are deliberately **not**
modelled here, because neither is a field on these surfaces.
`CardView.counters` is a `dict[str, int]` whose keys are counter kinds, not
model fields, and `ui/cards.js` walks it with `Object.entries`; the engine's
19 notice lists are keyed by name in `ui/app.js`'s
`ENGINE_GAP_NOTICE_MESSAGES` and already have their own drift guard in
`tests/test_notices.py`. Naming them would have meant enumerating something
this file does not publish.

What this rule can and cannot see, stated plainly rather than implied. It is
name-based, not type-flow-based: two different published models that share a
field name are indistinguishable, so a name read anywhere in `ui/` counts
everywhere. That is a deliberate lean towards false negatives on *common*
names (`id`, `name`, `cost`), and it is the right lean, because the drift
that caused all six bugs is a **newly published field name that nothing in
the UI mentions at all** - which this catches exactly. The alternative, a
guard that fires on a field someone can point at a live consumer for, is a
guard that gets deleted the first time it is wrong. When in doubt the answer
is an annotated allowlist entry, never a looser regex.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path
from typing import NamedTuple

import pytest
from pydantic import BaseModel

from magic_cards_model_api import schemas

_UI_DIR = Path(__file__).resolve().parents[1] / "ui"


def _ui_files() -> list[Path]:
    """Every browser module the served UI actually loads, in a stable order."""
    return sorted(_UI_DIR.glob("*.js")) + sorted((_UI_DIR / "render").glob("*.js"))


def strip_js_comments(text: str) -> str:
    """`text` with `/* ... */` and `// ...` removed.

    Deliberately naive - it does not know about comment markers inside string
    literals. The consequence of a false strip is a field reading as *not*
    consumed, which fails loudly and is fixed by an allowlist entry; the
    consequence of not stripping at all is a field mentioned in a docstring
    reading as wired, which is silent and wrong. This errs the safe way.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"(?m)//.*$", " ", text)


def is_consumed(name: str, source: str) -> bool:
    """Whether `source` (already comment-stripped) reads a property `name`.

    See the module docstring for the three shapes and for why an assignment
    (`.name =`) is excluded.
    """
    escaped = re.escape(name)
    patterns = (
        # A property read. `(?!\s*=(?!=))` drops `.name = ...` (a request
        # body being built) while keeping `.name === x` and `.name == x`.
        rf"\.{escaped}\b(?!\s*=(?!=))",
        # A bracket read with a literal key.
        rf"\[\s*[\"'`]{escaped}[\"'`]\s*\]",
        # A destructuring binding: `const {a, name} = ...`, `({name}) => ...`.
        rf"\{{[^{{}}\n]*\b{escaped}\b[^{{}}\n]*\}}\s*[=)]",
    )
    return any(re.search(pattern, source) for pattern in patterns)


@pytest.fixture(scope="module")
def ui_source() -> str:
    files = _ui_files()
    assert files, f"no browser modules under {_UI_DIR} - the served UI is missing"
    return "\n".join(strip_js_comments(path.read_text()) for path in files)


# -- the surfaces --------------------------------------------------------


#: The response models the browser actually reads, by the name this file
#: reports them under. Everything nested inside them is walked too, so a view
#: reached only through one of these (`CastHintsView` through `CardView.cast`,
#: `ObjectCostsView` through `AffordancesResponse`) is covered without being
#: named here.
_SURFACES = {
    "legal-actions row": schemas.ActionRow,
    "legal-actions row with candidates": schemas.ActionWithTargets,
    "card view": schemas.CardView,
    "stack ability view": schemas.StackAbilityView,
    "pending decision view": schemas.PendingDecisionView,
    "affordances": schemas.AffordancesResponse,
}


def _nested_models(annotation: object) -> list[type[BaseModel]]:
    found: list[type[BaseModel]] = []
    for argument in typing.get_args(annotation) or ():
        found += _nested_models(argument)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.append(annotation)
    return found


def _declaring_class(model: type[BaseModel], field: str) -> type[BaseModel]:
    """The class in `model`'s MRO that actually declares `field`.

    `ActionRow` inherits most of its wire from the engine's `Action`, and
    that inheritance is how three of the six bugs got published in the first
    place - so an entry here is keyed by where the field is *defined*, which
    is the file a future engine bump will touch.
    """
    for candidate in reversed(model.__mro__):
        if (
            isinstance(candidate, type)
            and issubclass(candidate, BaseModel)
            and field in getattr(candidate, "__annotations__", {})
        ):
            return candidate
    return model


class _Field(NamedTuple):
    owner: str
    """The class that declares the field."""
    name: str
    surfaces: tuple[str, ...]
    """Which entries of `_SURFACES` publish it, for the failure message."""


def published_fields() -> list[_Field]:
    """Every field on every surface, keyed by its declaring class."""
    where: dict[tuple[str, str], set[str]] = {}
    for label, root in _SURFACES.items():
        seen: set[type[BaseModel]] = set()
        queue = [root]
        while queue:
            model = queue.pop()
            if model in seen:
                continue
            seen.add(model)
            for name, info in model.model_fields.items():
                owner = _declaring_class(model, name).__name__
                where.setdefault((owner, name), set()).add(label)
                queue += _nested_models(info.annotation)
    return [
        _Field(owner, name, tuple(sorted(labels)))
        for (owner, name), labels in sorted(where.items())
    ]


# -- what is deliberately not consumed -----------------------------------


#: The vocabulary an allowlist reason must use. Not decoration: "cosmetic"
#: and "known-gap" are different promises, and an entry that cannot name
#: which one it is has not actually been thought about.
_REASON_KINDS = frozenset(
    {
        # Another published field already carries the same fact to the
        # browser, and that one IS read.
        "superseded",
        # Published for the MCP tools / AI seats, which are a different
        # client with different needs. The browser has no use for it.
        "agent-facing",
        # Real information the browser could show and does not. Nothing is
        # unplayable because of it.
        "cosmetic",
        # Nothing could consume it: the api has no request field that would
        # accept the answer, so there is nothing for the browser to wire to.
        "unreachable",
        # A genuine hole, named rather than dressed up. Something a player
        # can feel, that nobody is fixing in this pass.
        "known-gap",
    }
)


class _Reason(NamedTuple):
    kind: str
    why: str


#: `(declaring class, field) -> reason`. Every entry below was checked
#: against the browser source when it was written - the class that declares
#: the field, what the browser does instead, and which of the five kinds that
#: makes it. None of them is a guess, and "known-gap" is used where the
#: honest answer is that something is missing rather than unwanted.
_NOT_CONSUMED_ON_PURPOSE: dict[tuple[str, str], _Reason] = {
    ("AbilityView", "mana"): _Reason(
        "superseded",
        "the same boolean as `is_mana_ability` (`engine_api.list_abilities` computes both "
        "from `is_mana_ability`), and `ui/mana.js`/`ui/actions.js` read that one",
    ),
    ("AbilityView", "mana_amount"): _Reason(
        "agent-facing",
        "how much this ability adds right now, for an MCP seat weighing which source to "
        "tap; the browser leaves tapping to the engine's auto-tap and totals nothing",
    ),
    ("AbilityView", "mana_cost_value"): _Reason(
        "agent-facing",
        "the mana part of a filter ability's own activation cost, read together with "
        "`mana_amount` by agent seats; the browser's mana menu nets nothing against it",
    ),
    ("Action", "additional_cost_options"): _Reason(
        "superseded",
        "the engine's own copy of the enumeration the browser already announces from, "
        "`CastHintsView.additional_costs` (`ui/actions.js`'s `announceable`)",
    ),
    ("Action", "alternative_cost"): _Reason(
        "superseded",
        "the browser picks an alternative cost from `CardView.cast_options` and names the "
        "keyword back in the request body; the row's echo of it is read by nothing",
    ),
    ("Action", "attacker_id"): _Reason(
        "superseded",
        "blocker drafting happens on the board's own combat view in `ui/combat.js`, which "
        "already knows which attack each blocker is being put in front of",
    ),
    ("Action", "color_choice_count"): _Reason(
        "known-gap",
        "how many colours a mana ability takes; `ui/mana.js` asks for exactly one, so an "
        '"in any combination of colors" source cannot be driven from the browser yet',
    ),
    ("Action", "color_options"): _Reason(
        "superseded",
        "the browser offers a mana ability's colours from `AbilityView.mana_options`, which "
        "`affordances.ability_hints` builds from this same engine answer",
    ),
    ("Action", "decision_candidates"): _Reason(
        "superseded",
        "the browser answers a question from `pending_decision` itself, never from an "
        "`answer_decision` row - those rows are enumerated for agent seats",
    ),
    ("Action", "decision_player_candidates"): _Reason(
        "superseded",
        "the player half of the same pool, and the browser reads `pending_decision.targets` "
        "for it instead of an `answer_decision` row",
    ),
    ("Action", "decision_option"): _Reason(
        "superseded",
        "one enumerated legal answer per row; the browser renders a form per kind from "
        "`ui/decisions.js`'s `DECISION_KINDS` and posts its own answer",
    ),
    ("Action", "decision_seq"): _Reason(
        "superseded",
        "which pending question a row answers; the browser names "
        "`pending_decision.generation` back on `POST .../decide` instead",
    ),
    ("Action", "pays_with"): _Reason(
        "agent-facing",
        "the engine's report of which permanents a cost would tap - its own docstring says "
        "nothing reads one back in, and the browser leaves the tapping to auto-tap",
    ),
    ("Action", "produces"): _Reason(
        "superseded",
        "what a mana ability's printed text says it adds; the browser labels the ability "
        "from `AbilityView.text` and `mana_options`, which it already holds",
    ),
    ("Action", "restriction_understood"): _Reason(
        "superseded",
        "the browser shows a CR 106.6 restriction through `AbilityView.restriction` and "
        "`ManaPoolView.restricted`, both of which carry their own `understood` flag",
    ),
    ("Action", "splice_options"): _Reason(
        "unreachable",
        "no request model in this api announces splice at all - `CastRequest` has no splice "
        "parameter - so there is nothing for the browser to wire this pool to",
    ),
    ("TargetSlot", "player_candidates"): _Reason(
        "superseded",
        "the player half of a slot's pool; `affordances.target_candidates` folds it into "
        "the `TargetSlotCandidates.candidates` lines the browser actually reads",
    ),
    ("Action", "target_slots"): _Reason(
        "superseded",
        "the browser builds its target slots from `ActionTargets.slots` and from "
        "`CastHintsView.targeting.requirements`, which restate the same requirements",
    ),
    ("ActionRow", "printed_produces"): _Reason(
        "cosmetic",
        "the printed phrase for a mana row the board resolved differently; the browser's "
        "mana menu shows the ability text and the resolved amount",
    ),
    ("ActionTargets", "more_with"): _Reason(
        "agent-facing",
        "where an MCP seat fetches the candidates its byte budget cut; the browser reads "
        "candidates out of the same call it already made",
    ),
    ("TargetSlotCandidates", "truncated"): _Reason(
        "known-gap",
        "how many legal candidates lie past `affordances.CANDIDATE_SLOT_CAP`; the browser "
        "renders the capped list with nothing on screen saying it was capped",
    ),
    ("CastOptionView", "pays_mana_cost"): _Reason(
        "cosmetic",
        "whether this keyword option still pays the printed mana cost; the menu shows the "
        "option's own `cost` string, which already says what will be paid",
    ),
    ("ExilePileView", "turn_exiled"): _Reason(
        "cosmetic",
        "the turn a card landed in an exile pile; the browser labels a pile with its "
        "`marker` and owner and shows no turn",
    ),
    ("ManaPlanEntry", "adds"): _Reason(
        "agent-facing",
        "one entry of `Action.pays_with`, which this same list already exempts for the same "
        "reason - no entry point takes a plan back in",
    ),
    ("ModalHintsView", "entwine_cost"): _Reason(
        "known-gap",
        "CR 702.42a's entwine cost is published on the modal hints and nothing in the "
        "browser offers it; its announceable form is a row `optional_costs` entry, itself "
        "still unread (see `_PENDING_SIBLING_FIXES`)",
    ),
    ("ModalHintsView", "escalate_cost"): _Reason(
        "known-gap",
        "CR 702.120a's escalate cost, in exactly the position `entwine_cost` is in - "
        "published on the hints, offered by no control, announceable only through the row",
    ),
    ("ObjectCostCandidateView", "pays"): _Reason(
        "known-gap",
        "which cost component a pitched card answers (exile, sacrifice, discard, tap); the "
        "browser lists the candidates without saying which half of the cost each covers",
    ),
    ("PendingDecisionView", "answer_field"): _Reason(
        "agent-facing",
        "which MCP `decide` parameter carries this kind's answer; the browser posts the "
        "field its own renderer built and never looks the mapping up",
    ),
    ("PendingDecisionView", "answer_with"): _Reason(
        "agent-facing",
        "a paste-ready MCP call answering this exact question - a string for an agent's "
        "transcript, not anything a browser would render",
    ),
    ("PendingDecisionView", "default_cards"): _Reason(
        "known-gap",
        "the engine's own suggested arrangement for a scry/surveil-shaped question; the "
        "decision window starts from `cards` in the order given and offers no one-click "
        "default",
    ),
    ("PendingDecisionView", "mana_sources"): _Reason(
        "known-gap",
        "the sources the engine would accept while paying (CR 608.2g); `ui/mana.js` builds "
        "its own list from the board's untapped permanents instead of reading this one",
    ),
    ("PendingDecisionView", "trample"): _Reason(
        "superseded",
        "the engine already puts the defending player or planeswalker in `targets` (with "
        "`lethal_damage` 0) exactly when trample lets damage reach it, so "
        "`assignDamageRows` carries the fact without the flag",
    ),
    ("SpecialActionEntry", "time_counters"): _Reason(
        "cosmetic",
        "how many time counters a suspend would start with; the special-action menu shows "
        "the cost and nothing else",
    ),
    ("StackAbilityView", "target_refs"): _Reason(
        "superseded",
        "the stack row renders `targets`, the display-string list published beside it; "
        "`target_refs` is the id-carrying form for a client that resolves its own names",
    ),
    ("TargetRequirementView", "mode_text"): _Reason(
        "cosmetic",
        "a modal requirement's own mode text; `ui/targeting.js` labels the slot "
        '"Mode N: <phrase>" from `mode` and `phrase`',
    ),
    ("TargetRequirementView", "variable_count"): _Reason(
        "known-gap",
        'whether a requirement\'s count is itself variable ("X target creatures"); '
        "`ui/targeting.js` branches on `count` and `unbounded` only",
    ),
    ("_CardViewFields", "art_crop"): _Reason(
        "cosmetic",
        "a third image crop; `ui/cards.js` picks between `image` and `small_image` and has "
        "no surface that wants a cropped art box",
    ),
    ("_CardViewFields", "face_up"): _Reason(
        "superseded",
        "`visibility._redact_facedown_card` already replaces a hidden card's whole view "
        'with a stub named "a face-down card", so the browser renders the difference '
        "without reading the flag",
    ),
    ("_CardViewFields", "keywords"): _Reason(
        "cosmetic",
        "the computed keyword list; the browser shows `oracle_text` plus the badges it "
        "derives from power, toughness, damage and counters",
    ),
    ("_CardViewFields", "mana_value"): _Reason(
        "agent-facing",
        "CR 202.3e's stack-aware mana value, for a seat reasoning about mana value; the "
        "browser renders `mana_cost` symbols and `x_value`",
    ),
    ("_CardViewFields", "printed_type_line"): _Reason(
        "cosmetic",
        "present only when a type-changing effect moved the type line; the browser shows "
        "the current `type_line` and never contrasts the two",
    ),
    ("_CardViewFields", "target_refs"): _Reason(
        "superseded",
        "the id-carrying form of `targets`, which is the display-string list the board and "
        "the review timeline already render",
    ),
}


class _GenericConsumer(NamedTuple):
    owner: str
    fields: tuple[str, ...]
    path: str
    proof: str
    why: str


#: Fields read as *keys* rather than by name. Each one names the snippet that
#: does the reading, and that snippet is asserted present - so this can never
#: become a blanket excuse for a consumer somebody deleted.
_CONSUMED_GENERICALLY: tuple[_GenericConsumer, ...] = (
    _GenericConsumer(
        owner="SpecialActionsView",
        fields=("plot", "foretell", "suspend"),
        path="app.js",
        proof="Object.keys(card.special_actions",
        why=(
            "`planHandCardClick` takes every key of `special_actions` except `cycling` and "
            "turns it into a menu item, so a new CR 116.2 special action is offered the day "
            "the api publishes it - none of these three names appears in ui/ at all"
        ),
    ),
)


class _Pending(NamedTuple):
    task: str
    why: str


#: Fields that are **bugs, not exemptions**: published, unread, and being
#: wired up right now by a sibling task in this same wave. They are listed
#: here instead of in `_NOT_CONSUMED_ON_PURPOSE` so that nothing about them
#: reads as deliberate.
#:
#: The entry is self-clearing, and that is the point: the moment the sibling
#: fix lands and the browser starts reading the field,
#: `test_pending_fixes_have_not_landed_yet` fails and names the line to
#: delete. A "known-pending" entry that passed forever would be an excuse
#: with extra steps.
_PENDING_SIBLING_FIXES: dict[tuple[str, str], _Pending] = {
    ("Action", "land_face"): _Pending(
        "U1 (cast path correctness)",
        "CR 712.12's MDFC land face: the row says which face is the land drop and the "
        "browser plays the card instead - the bug where a card was burned as a land",
    ),
    ("CardFaceView", "land_face"): _Pending(
        "U1 (cast path correctness)",
        "the same MDFC flag on the card's own face list, which is where the hand card has "
        "to read it to offer the land drop at all",
    ),
    ("_CardViewFields", "faces"): _Pending(
        "U1 (cast path correctness)",
        "CR 712's face list; the browser has no double-faced card display at all today, and "
        "the MDFC land-drop fix is the first thing that needs one",
    ),
    ("Action", "spell_face"): _Pending(
        "U1 (cast path correctness)",
        "CR 709.3's split half and CR 715.3's Adventure: each face is its own row with its "
        "own cost, and a browser that ignores the field casts the wrong half",
    ),
    ("ActionRow", "unreadable_modes"): _Pending(
        "U1 (cast path correctness)",
        "CR 700.2a modes this engine cannot read, refused on cast - the bug where a mode "
        "was accepted through targeting and only then refused",
    ),
    ("UnreadableModeView", "mode_number"): _Pending(
        "U1 (cast path correctness)",
        "which mode of the printed list is unreadable - the number the mode picker has to "
        "disable, reachable only once `unreadable_modes` is read",
    ),
    ("UnreadableModeView", "notice"): _Pending(
        "U1 (cast path correctness)",
        "the engine's own words for why that mode would do nothing, which is what the mode "
        "picker should be saying instead of refusing after the fact",
    ),
    ("Action", "x_range"): _Pending(
        "U2 (cost surfaces)",
        "CR 107.3a's legal X range; the browser's X input is bounded by nothing, so a spell "
        "can be announced at an X the engine will refuse",
    ),
    ("ActionRow", "x_costs"): _Pending(
        "U2 (cost surfaces)",
        "what each X actually charges once a floor or a reduction has eaten into it - the "
        "half of the wrong-price bug the plain `cost` string cannot express",
    ),
    ("ActionRow", "printed_cost"): _Pending(
        "U2 (cost surfaces)",
        "the cost the card would print when CR 601.2f changed what it is charged; a browser "
        "showing only one of the two shows the player the wrong price",
    ),
    ("ActionRow", "optional_costs"): _Pending(
        "U2 (cost surfaces)",
        "the reported kicker bug: the row's own optional costs are never read - the browser "
        "writes `body.optional_costs` from the card hints and reads the row's back never",
    ),
    ("ActionRow", "improvise"): _Pending(
        "U2 (cost surfaces)",
        "CR 702.126a's artifacts to tap; the row says the cast is payable once they are, "
        "and a browser that ignores it calls an artifact deck uncastable",
    ),
    ("ObjectCostsView", "improvise"): _Pending(
        "U2 (cost surfaces)",
        "the affordance half of the same offer - which untapped artifacts could be spent, "
        "beside convoke, delve and crew, which the browser already reads",
    ),
    ("ActionRow", "activate_first"): _Pending(
        "U2 (cost surfaces)",
        "CR 118.3c mana abilities that must be activated by hand before a row the engine "
        'calls "affordable: yes" can actually be paid',
    ),
    ("ManualManaStepView", "adds"): _Pending(
        "U2 (cost surfaces)",
        "what one of those manual activations adds, reachable only once `activate_first` "
        "itself is read",
    ),
}


# -- the guard -----------------------------------------------------------


def _generic_index() -> dict[tuple[str, str], _GenericConsumer]:
    return {
        (entry.owner, field): entry for entry in _CONSUMED_GENERICALLY for field in entry.fields
    }


def test_every_published_field_is_consumed_or_written_down(ui_source: str) -> None:
    """The guard itself. A field published on a surface the browser reads is
    read by `ui/`, declared as a generic read, listed as a sibling task's
    in-flight fix, or written down with a reason - and nothing else."""
    generic = _generic_index()
    unexplained = []
    for field in published_fields():
        key = (field.owner, field.name)
        if key in generic or key in _NOT_CONSUMED_ON_PURPOSE or key in _PENDING_SIBLING_FIXES:
            continue
        if is_consumed(field.name, ui_source):
            continue
        unexplained.append(
            f"{field.owner}.{field.name} (published on: {', '.join(field.surfaces)})"
        )
    assert not unexplained, (
        "these fields are published to the browser and nothing under ui/ reads them:\n  "
        + "\n  ".join(unexplained)
        + "\n\nThis is the shape of all six of the bugs this guard exists for. Do one of:\n"
        "  1. wire the field up in ui/ (the usual answer for a field just added), or\n"
        "  2. if ui/ DOES read it generically (by key, not by name), add it to\n"
        "     _CONSUMED_GENERICALLY with the snippet that reads it, or\n"
        "  3. if the browser deliberately does not want it, add it to\n"
        "     _NOT_CONSUMED_ON_PURPOSE with a reason kind and one line saying why.\n"
        "Do not widen is_consumed() to make this pass - see the module docstring."
    )


def test_allowlisted_fields_are_still_published_and_still_unread(ui_source: str) -> None:
    """The allowlist rots in two ways and both are caught here: an entry for a
    field that no longer exists, and an entry for a field the browser has
    since learned to read."""
    published = {(field.owner, field.name) for field in published_fields()}
    gone = sorted(
        f"{owner}.{name}"
        for owner, name in _NOT_CONSUMED_ON_PURPOSE
        if (owner, name) not in published
    )
    assert not gone, (
        f"_NOT_CONSUMED_ON_PURPOSE names fields that are no longer published: {gone} - "
        "delete the entries"
    )
    wired = sorted(
        f"{owner}.{name}"
        for owner, name in _NOT_CONSUMED_ON_PURPOSE
        if is_consumed(name, ui_source)
    )
    assert not wired, (
        f"ui/ now reads {wired}, which _NOT_CONSUMED_ON_PURPOSE says it deliberately does "
        "not - delete those entries so the guard keeps watching the field for real"
    )


def test_every_allowlist_entry_is_annotated() -> None:
    """Clause (b) of the brief, enforced: an entry with no reason, or with a
    reason kind nobody defined, is not documentation."""
    for (owner, name), reason in sorted(_NOT_CONSUMED_ON_PURPOSE.items()):
        assert reason.kind in _REASON_KINDS, (
            f"{owner}.{name} has reason kind {reason.kind!r}, which is not one of "
            f"{sorted(_REASON_KINDS)}"
        )
        assert len(reason.why.split()) >= 6, (
            f"{owner}.{name}'s reason is too short to be a reason: {reason.why!r}"
        )


def test_every_generic_consumer_is_still_in_the_browser() -> None:
    """A declared generic read is only an answer while the code that does the
    reading is still there. Delete `Object.keys(card.special_actions)` and
    this fails, instead of `plot`/`foretell`/`suspend` silently keeping an
    exemption they no longer earn."""
    published = {(field.owner, field.name) for field in published_fields()}
    for entry in _CONSUMED_GENERICALLY:
        path = _UI_DIR / entry.path
        assert path.is_file(), f"{entry.path} is gone, but {entry.owner} still points at it"
        source = strip_js_comments(path.read_text())
        assert entry.proof in source, (
            f"{entry.owner}.{entry.fields} is exempt because {entry.path} reads it generically "
            f"({entry.proof!r}), and that snippet is no longer in the file - either the fields "
            "are now dead or the read moved; do not update this string without checking which"
        )
        for field in entry.fields:
            assert (entry.owner, field) in published, (
                f"{entry.owner}.{field} is no longer published - delete it from "
                "_CONSUMED_GENERICALLY"
            )


def test_pending_fixes_have_not_landed_yet(ui_source: str) -> None:
    """`_PENDING_SIBLING_FIXES` is a promise that someone else is wiring the
    field up. When they do, this fails and names the entry to delete - which
    is the only thing that keeps a "pending" entry from becoming a permanent
    exemption nobody re-reads."""
    published = {(field.owner, field.name) for field in published_fields()}
    landed = sorted(
        f"{owner}.{name} ({entry.task})"
        for (owner, name), entry in _PENDING_SIBLING_FIXES.items()
        if is_consumed(name, ui_source)
    )
    assert not landed, (
        f"ui/ now reads {landed} - the fix landed. Delete those entries from "
        "_PENDING_SIBLING_FIXES in tests/test_ui_surface_coverage.py; the field is "
        "guarded by the ordinary rule from here on."
    )
    gone = sorted(
        f"{owner}.{name}"
        for owner, name in _PENDING_SIBLING_FIXES
        if (owner, name) not in published
    )
    assert not gone, f"_PENDING_SIBLING_FIXES names fields that are no longer published: {gone}"
    for (owner, name), entry in sorted(_PENDING_SIBLING_FIXES.items()):
        assert entry.task, f"{owner}.{name} is pending on nothing - name the task"
        assert len(entry.why.split()) >= 6, f"{owner}.{name}'s pending note is too short"


# -- the detector's own rules, pinned ------------------------------------
#
# The guard is only as good as `is_consumed`, so its three load-bearing
# decisions are tested directly rather than left to be re-derived from the
# real source the next time someone is tempted to widen them.


def test_a_field_named_only_in_a_comment_is_not_consumed() -> None:
    source = strip_js_comments(
        "// which keywords are alternative costs is the engine's\nconst a = 1;"
    )
    assert not is_consumed("keywords", source)
    assert not is_consumed("costs", source)


def test_a_block_comment_mention_is_not_consumed() -> None:
    source = strip_js_comments("/** reads `row.printed_cost` one day */\nconst a = 1;")
    assert not is_consumed("printed_cost", source)


def test_building_a_request_body_is_not_consuming_the_response_field() -> None:
    """The distinction three fields hang on, `optional_costs` among them."""
    assert not is_consumed(
        "optional_costs", "if (announced.length) body.optional_costs = announced;"
    )
    assert is_consumed("optional_costs", "if (row.optional_costs.length) show(row);")
    assert is_consumed("affordable", 'if (row.affordable === "no") disable();')


def test_bracket_and_destructuring_reads_count() -> None:
    assert is_consumed("land_face", 'const face = card["land_face"];')
    assert is_consumed("land_face", "const { land_face } = card;")
    assert is_consumed("land_face", "render(({ land_face }) => land_face);")


def test_a_bare_word_is_not_a_read() -> None:
    """`Array.prototype.splice` must not launder `Action.splice_options`, and
    a bare identifier is not a property read."""
    assert not is_consumed("splice_options", "next.splice(index, 0, id);")
    assert not is_consumed("improvise", "const improvise = 1;")
