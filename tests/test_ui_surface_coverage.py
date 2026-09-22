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

5. **A read only counts on a view the browser can reach.** A nested view
   arrives inside a field, so a field nobody reads is a view nobody holds:
   `UnreadableModeView` is published only inside `ActionRow.unreadable_modes`,
   so for as long as nothing reads *that*, no line in `ui/` can be reading
   `UnreadableModeView.notice`, whatever the name says - and `ui/render/
   ribbon.js` does read a `.notice`, on `ModeOptionView`. (U2 has since
   wired the row list in `ui/menus.js`, which is what makes all three of
   those keys land together rather than a name collision clearing one of
   them early.) `reachable_models` walks the surfaces as a graph, following
   an edge only when the field labelling it is read (by name, or generically
   as in 4), and `is_reached` is `is_consumed` plus "and at least one view
   carrying this field is reachable".

Two other generic reads the sweep turned up are deliberately **not**
modelled here, because neither is a field on these surfaces.
`CardView.counters` is a `dict[str, int]` whose keys are counter kinds, not
model fields, and `ui/cards.js` walks it with `Object.entries`; the engine's
19 notice lists are keyed by name in `ui/app.js`'s
`ENGINE_GAP_NOTICE_MESSAGES` and already have their own drift guard in
`tests/test_notices.py`. Naming them would have meant enumerating something
this file does not publish.

What this rule can and cannot see, stated plainly rather than implied. It is
name-based, not type-flow-based: two published models that share a field
name are indistinguishable to rule 2, so a name read anywhere in `ui/` would
otherwise count everywhere. On the ordinary guard that is a deliberate lean
towards false negatives on *common* names (`id`, `name`, `cost`), and it is
the right lean, because the drift that caused all six bugs is a **newly
published field name that nothing in the UI mentions at all** - which this
catches exactly. A guard that fires on a field someone can point at a live
consumer for is a guard that gets deleted the first time it is wrong. When in
doubt the answer is an annotated allowlist entry, never a looser regex.

That lean flips sign on the two **self-clearing** lists
(`_NOT_CONSUMED_ON_PURPOSE`, `_PENDING_SIBLING_FIXES`), which react to a read
by telling a human to delete an entry: there a false positive deletes a
guard. `U2 (cost surfaces)` hit that for real - it publishes
`ModeOptionView.notice` and reads it as `m.notice`, which by name alone
"clears" the `UnreadableModeView.notice` entry on a view nothing could reach
until the same task wired `ActionRow.unreadable_modes`. Read-by-name alone,
`.notice` would have cleared one of that trio's three entries a commit
before the other two, which is a promise deleted rather than kept. Rule 5 is
the answer: `test_a_collision_on_an_unreachable_view_does_not_clear_a_promise`
pins that exact merge, and `test_reading_the_row_list_does_clear_the_whole_
promise` is its other half - the rule has to let a kept promise clear too.
Rule 5 is *not* a licence to widen 2: the residual it
does not cover - a collision between two views the browser genuinely holds
both of - stays a known false negative, to be answered by reading the field
or writing an entry, never by loosening the regex.

Rule 5 subtracts from the ordinary guard too: a field on an unreachable view
is skipped rather than reported, because the fix is to wire up the link, not
to write a second entry per field behind it.
`test_every_unreachable_view_is_anchored_in_a_written_down_link` is what
keeps that from being a hole - every unreachable view must hang off a link
that is itself written down, so a new view behind a new unread field fails
twice, not zero times.

An exemption is not available for a field the brief called a bug
----------------------------------------------------------------
Clause (d) of the brief that asked for this file names six fields - MDFC
`land_face`, `x_range`, `spell_face`, `improvise`, `unreadable_modes`,
`optional_costs` - and says of them, in those words, that they "must NOT be
allowlisted - they are bugs, not exemptions". Two of them ended up in
`_NOT_CONSUMED_ON_PURPOSE` anyway, one round apart, each with a true sentence
attached: `Action.x_range` as a `known-gap` whose own text conceded the live
defect it was excusing, and `ActionRow.optional_costs` as `superseded` by the
cast hints the strip offers its chips from. Neither reason was wrong about
the browser. Both were answers to a question the brief had already closed.

Prose cannot hold that line, because the failure mode *is* a later reader
finding a defensible sentence. `_NAMED_AS_BUGS_BY_THE_BRIEF` holds it
instead, with two tests that have no answer between them but wiring the
field up: a named field may not appear in the exemption list at all
(`test_a_field_the_brief_named_as_a_bug_is_never_an_exemption`), and it must
be read by `ui/` or still owed by a task that has shipped none of its own
fields (`test_every_field_the_brief_named_as_a_bug_is_read_or_still_owed`).
Both `x_range` and `optional_costs` are wired now - the X box carries the
engine's own floor and ceiling, and the additional-cost step carries its own
per-option price - so the list guards them rather than describing them.

A pending entry may not outlive its task
----------------------------------------
`_PENDING_SIBLING_FIXES` is the one entry shape that is a promise rather
than a state: "published, unread, and somebody else is wiring it". A promise
rots in a way a reason does not, and it did. The first round of this file
parked fifteen fields on two sibling tasks; nine of them were wired and six
were not, and the six sat in a list that said they were in flight. Nothing
failed, because each entry's only self-clearing trigger was the field being
*read* - which is exactly what does not happen to a field its owner walked
away from.

A promise also rots by being parked on the wrong owner, which is the second
thing that happened here. `ActionRow.unreadable_modes` and the two
`UnreadableModeView` fields behind it were parked on U1 and delivered by U2,
in `ui/menus.js` rather than on the ribbon; a round of this file read the
gap as "nobody is coming for these" and wrote them into
`_NOT_CONSUMED_ON_PURPOSE` as `superseded` - an exemption asserting the
browser deliberately does not read three fields it had begun reading
fourteen minutes earlier. Pinning that collision on the *allowlist* is what
`test_a_collision_on_an_unreachable_view_does_not_clear_a_promise` and
`test_reading_the_row_list_does_clear_the_whole_promise` do below, against a
synthetic pending dict rather than the live one - the mechanism is worth
keeping pinned even once the wave that exposed it has fully landed.

Three rules held the shape while `_PENDING_SIBLING_FIXES` was non-empty, and
none of them needed anybody to remember anything:

* an entry must name a task in `_OPEN_WAVE_TASKS`
  (`test_every_pending_entry_names_a_wave_that_is_still_open`);
* a task in `_OPEN_WAVE_TASKS` must still be named by an entry
  (`test_every_open_wave_still_has_a_pending_entry`), so the declaration and
  the parking die together and neither can rot alone;
* and the one that does not wait for a human act at all
  (`abandoned_pending_entries`): a task that has wired **some** of its fields
  has shipped, so whatever it left unread is parked on nobody and is reported
  the moment the first of its fields lands.

All three rules still hold with the dict and the task tuple both empty - each
one degenerates to "nothing to check" rather than to a false pass, which is
what lets `_PENDING_SIBLING_FIXES`/`_OPEN_WAVE_TASKS` repopulate for a future
wave without anybody having to re-derive the shape.

What remains deliberate, and is the whole point of a *self*-clearing list: a
landed entry still fails, once, with the exact keys to delete
(`test_pending_fixes_have_not_landed_yet`). A landed entry that passed would
be a guard quietly downgraded from "unread and unexplained, fail" to
"unread and excused" the day somebody deletes the read. U1 (cast path
correctness) and U2 (cost surfaces) have now each shipped every field they
owed - that is what emptied both lists - so the "next merge" simulation this
file used to run against a live wave (`_WAVE_LANDING_READS`, and the two
tests that measured against it) went with them: nothing is in flight to
simulate landing.
"""

from __future__ import annotations

import re
import typing
from collections.abc import Mapping, Sequence
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
    containers: tuple[type[BaseModel], ...]
    """The models that carry it on the wire, for `reachable_models`. Usually
    one; `Action`'s fields ride both `ActionRow` and `ActionWithTargets`."""


def _views() -> dict[type[BaseModel], dict[str, tuple[type[BaseModel], ...]]]:
    """Every model on a browser-facing surface, and for each of its fields the
    views that field carries. This is the wire as a graph: the roots of
    `_SURFACES` plus everything reachable from them, with the edges labelled
    by the field name the browser would have to read to follow them."""
    views: dict[type[BaseModel], dict[str, tuple[type[BaseModel], ...]]] = {}
    queue = list(_SURFACES.values())
    while queue:
        model = queue.pop()
        if model in views:
            continue
        views[model] = {}
        for name, info in model.model_fields.items():
            nested = tuple(dict.fromkeys(_nested_models(info.annotation)))
            views[model][name] = nested
            queue += nested
    return views


def published_fields() -> list[_Field]:
    """Every field on every surface, keyed by its declaring class."""
    where: dict[tuple[str, str], set[str]] = {}
    carried: dict[tuple[str, str], set[type[BaseModel]]] = {}
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
                carried.setdefault((owner, name), set()).add(model)
                queue += _nested_models(info.annotation)
    return [
        _Field(
            owner,
            name,
            tuple(sorted(labels)),
            tuple(sorted(carried[owner, name], key=lambda model: model.__name__)),
        )
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
    ("ActionRow", "acting_for"): _Reason(
        "known-gap",
        "a controlled player's row offered to their controller (CR 723.5); the browser does "
        "not yet act for another player - an AI seat controlling a human is the live case",
    ),
    ("ActionRow", "activate_first"): _Reason(
        "known-gap",
        "CR 118.3c mana abilities a row the engine already calls affordable cannot be paid "
        "without; `ui/menus.js` can activate a permanent's mana ability from the board and "
        "nothing on the casting strip says which activations this row is waiting on",
    ),
    ("ActionRow", "printed_cost"): _Reason(
        "cosmetic",
        "the cost the card would print where CR 601.2f changed what it is charged; the "
        "casting strip shows no price text at all - the dry run highlights the permanents "
        "the cast will tap - so the printed-versus-charged contrast has nowhere to go, and "
        "the cast is charged `cost` either way",
    ),
    ("ActionRow", "printed_produces"): _Reason(
        "cosmetic",
        "the printed phrase for a mana row the board resolved differently; the browser's "
        "mana menu shows the ability text and the resolved amount",
    ),
    ("ActionRow", "x_costs"): _Reason(
        "known-gap",
        "what an X row is charged at each X its `cost` string cannot describe once a floor "
        "or a reduction has eaten into it; `renderXInput` now bounds the box by the row's "
        "`x_range`, which is what the engine will accept, but no price per X is shown and "
        "an unaffordable X inside that range is still found by dry-running it",
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
    ("ManualManaStepView", "adds"): _Reason(
        "known-gap",
        "what one of `ActionRow.activate_first`'s manual activations adds on a copy of the "
        "board - the same hole one step in, and reachable only once that field is read",
    ),
    ("ModalHintsView", "entwine_cost"): _Reason(
        "known-gap",
        "CR 702.42a's entwine cost is published on the modal hints and nothing in the "
        "browser offers it; the strip builds its chips from `CastHintsView.additional_"
        "costs`, which does not carry the modal hints' own two",
    ),
    ("ModalHintsView", "escalate_cost"): _Reason(
        "known-gap",
        "CR 702.120a's escalate cost, in exactly the position `entwine_cost` is in - "
        "published on the hints and offered by no control",
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


#: The sibling tasks of the wave currently in flight, by the name their
#: `_PENDING_SIBLING_FIXES` entries give them. This is the one place that
#: says "somebody is still working on this", and it is what stops a pending
#: entry from outliving the task it names.
#:
#: The two lists are pinned to each other in both directions - an entry must
#: name a task listed here (`test_every_pending_entry_names_a_wave_that_is_
#: still_open`) and a task listed here must still have an entry
#: (`test_every_open_wave_still_has_a_pending_entry`) - so the declaration
#: and the parking die together rather than the tuple rotting on its own.
#:
#: Emptying it is not the only guard against a task that quietly finishes,
#: and deliberately so, because emptying it is a human act. The one that is
#: not is `abandoned_pending_entries`: a task that has wired *some* of its
#: fields has shipped, so the rest of its entries are not pending on
#: anybody, and they are reported the moment the first one lands. That is
#: the check the last round of this wave was missing. Of the fifteen fields
#: the first round parked, all seven on U1 ended up read - four by U1, three
#: by U2 in `ui/menus.js`, which is the mis-parking the module docstring
#: describes - while U2 wired two of its own eight, and the six it left read
#: as "in flight" forever.
_OPEN_WAVE_TASKS: tuple[str, ...] = ()


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
#:
#: "Reading the field" means `is_reached`, not a bare name hit - see rule 5
#: in the module docstring. An entry can sit on a nested view that only
#: exists inside another entry here (`CardFaceView.land_face` inside
#: `_CardViewFields.faces`), and a sibling wave that happens to publish the
#: same field name on some other view must not delete it.
#:
#: An entry is a promise, so it is held to the three rules in the module
#: docstring's "A pending entry may not outlive its task": it names an open
#: task, that task is still named here, and a task that has wired some of
#: its fields has shipped and may not park the rest. A field whose owner is
#: gone belongs in `_NOT_CONSUMED_ON_PURPOSE` with a reason kind, or in
#: `ui/`.
#: Empty: U1 (cast path correctness) and U2 (cost surfaces) - the two tasks
#: that parked fields here - have each shipped every field they owed. A
#: future wave that publishes a field one sibling task reads and another has
#: not yet wired repopulates this the same way; until then the ordinary rule
#: (`_NOT_CONSUMED_ON_PURPOSE`, or a genuine unread field) covers everything.
_PENDING_SIBLING_FIXES: dict[tuple[str, str], _Pending] = {}


# -- the guard -----------------------------------------------------------


def _generic_index() -> dict[tuple[str, str], _GenericConsumer]:
    return {
        (entry.owner, field): entry for entry in _CONSUMED_GENERICALLY for field in entry.fields
    }


def _link_is_read(
    model: type[BaseModel],
    name: str,
    ui_source: str,
    generic: dict[tuple[str, str], _GenericConsumer],
) -> bool:
    """Whether the browser follows the edge `model.name` at all - by name, or
    through a declared generic read (`SpecialActionsView`'s keys, which is how
    `SpecialActionEntry` is reached without any of its link names appearing)."""
    return (_declaring_class(model, name).__name__, name) in generic or is_consumed(name, ui_source)


def reachable_models(ui_source: str) -> set[type[BaseModel]]:
    """The views the browser can actually get to.

    A surface root is reachable. Anything else is reachable only when some
    field carrying it is itself read - because a nested view arrives inside a
    field, and a field the browser never reads is a view the browser never
    holds. See the module docstring for why this, and not a wider regex, is
    the answer to a name that two views share."""
    views = _views()
    generic = _generic_index()
    reachable = set(_SURFACES.values())
    frontier = list(reachable)
    while frontier:
        model = frontier.pop()
        for name, nested in views[model].items():
            if not nested or not _link_is_read(model, name, ui_source, generic):
                continue
            for view in nested:
                if view not in reachable:
                    reachable.add(view)
                    frontier.append(view)
    return reachable


def is_reached(field: _Field, ui_source: str, reachable: set[type[BaseModel]]) -> bool:
    """Whether `ui/` reads this field *as published here*: the name is read,
    and at least one of the views that carries it is one the browser holds."""
    return any(model in reachable for model in field.containers) and is_consumed(
        field.name, ui_source
    )


def _reached_index(ui_source: str) -> dict[tuple[str, str], bool]:
    reachable = reachable_models(ui_source)
    return {
        (field.owner, field.name): is_reached(field, ui_source, reachable)
        for field in published_fields()
    }


def landed_pending_entries(
    ui_source: str, pending: Mapping[tuple[str, str], _Pending] | None = None
) -> list[tuple[str, str]]:
    """The pending keys whose sibling fix has landed."""
    reached = _reached_index(ui_source)
    entries = _PENDING_SIBLING_FIXES if pending is None else pending
    return sorted(key for key in entries if reached.get(key))


def orphaned_pending_entries(
    pending: Mapping[tuple[str, str], _Pending], open_tasks: Sequence[str]
) -> list[tuple[str, str]]:
    """The pending keys parked on a task nobody has declared open."""
    return sorted(key for key, entry in pending.items() if entry.task not in open_tasks)


def waves_with_nothing_pending(
    pending: Mapping[tuple[str, str], _Pending], open_tasks: Sequence[str]
) -> list[str]:
    """The declared-open tasks no pending entry names any more."""
    parked = {entry.task for entry in pending.values()}
    return sorted(task for task in open_tasks if task not in parked)


def abandoned_pending_entries(
    pending: Mapping[tuple[str, str], _Pending], ui_source: str
) -> list[tuple[str, str]]:
    """The pending keys whose task has demonstrably finished without them.

    A task that has wired *none* of its fields is still in flight, and its
    entries are exactly what the pending list is for. A task that has wired
    at least one has shipped: whatever it left unread it is not going to
    come back for, so those entries are parked on nobody and are reported
    here. This is the check that does not wait for a human to remember to
    empty `_OPEN_WAVE_TASKS`."""
    reached = _reached_index(ui_source)
    by_task: dict[str, list[tuple[str, str]]] = {}
    for key, entry in pending.items():
        by_task.setdefault(entry.task, []).append(key)
    abandoned: list[tuple[str, str]] = []
    for keys in by_task.values():
        if not any(reached.get(key) for key in keys):
            continue
        abandoned += [key for key in keys if not reached.get(key)]
    return sorted(abandoned)


def wired_allowlist_entries(ui_source: str) -> list[tuple[str, str]]:
    """The `_NOT_CONSUMED_ON_PURPOSE` keys the browser has since learned to read."""
    reached = _reached_index(ui_source)
    return sorted(key for key in _NOT_CONSUMED_ON_PURPOSE if reached.get(key))


def test_every_published_field_is_consumed_or_written_down(ui_source: str) -> None:
    """The guard itself. A field published on a surface the browser reads is
    read by `ui/`, declared as a generic read, listed as a sibling task's
    in-flight fix, or written down with a reason - and nothing else."""
    generic = _generic_index()
    reachable = reachable_models(ui_source)
    unexplained = []
    for field in published_fields():
        key = (field.owner, field.name)
        if key in generic or key in _NOT_CONSUMED_ON_PURPOSE or key in _PENDING_SIBLING_FIXES:
            continue
        if not any(model in reachable for model in field.containers):
            # Every view carrying it arrives inside a field the browser does
            # not read, so the answer for this field is the entry on that
            # link, not a second entry here. The link cannot itself be
            # unaccounted for - `test_every_unreachable_view_is_anchored...`
            # is what holds that door shut.
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
    wired = [f"{owner}.{name}" for owner, name in wired_allowlist_entries(ui_source)]
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


def test_every_unreachable_view_is_anchored_in_a_written_down_link(ui_source: str) -> None:
    """The one hole reachability could have opened, nailed shut.

    Fields on a view the browser cannot reach are skipped by the ordinary
    rule, on the grounds that the link carrying that view is the thing with
    the entry against it. This asserts that is true: every unreachable view
    is reached from a reachable one by at least one link that is written down
    in `_NOT_CONSUMED_ON_PURPOSE` or `_PENDING_SIBLING_FIXES`. Publish a new
    nested view behind a new unread field and its fields do not vanish - the
    field itself fails the ordinary rule, and this fails too until somebody
    says which it is."""
    views = _views()
    reachable = reachable_models(ui_source)
    written_down = set(_NOT_CONSUMED_ON_PURPOSE) | set(_PENDING_SIBLING_FIXES)

    anchored: set[type[BaseModel]] = set()
    frontier = sorted(reachable, key=lambda model: model.__name__)
    while frontier:
        model = frontier.pop()
        for name, nested in views[model].items():
            if (_declaring_class(model, name).__name__, name) not in written_down:
                continue
            for view in nested:
                if view not in reachable and view not in anchored:
                    anchored.add(view)
                    frontier.append(view)

    orphans = sorted(
        view.__name__ for view in views if view not in reachable and view not in anchored
    )
    assert not orphans, (
        f"these views are published and unreachable from ui/: {orphans} - and no field "
        "carrying one is written down, so their own fields are being skipped for no "
        "recorded reason. Wire up the field that carries the view, or give that field an "
        "entry in _NOT_CONSUMED_ON_PURPOSE or _PENDING_SIBLING_FIXES."
    )


def test_pending_fixes_have_not_landed_yet(ui_source: str) -> None:
    """`_PENDING_SIBLING_FIXES` is a promise that someone else is wiring the
    field up. When they do, this fails and names the entry to delete - which
    is the only thing that keeps a "pending" entry from becoming a permanent
    exemption nobody re-reads."""
    published = {(field.owner, field.name) for field in published_fields()}
    landed = [
        f"{owner}.{name} ({_PENDING_SIBLING_FIXES[owner, name].task})"
        for owner, name in landed_pending_entries(ui_source)
    ]
    assert not landed, (
        f"ui/ now reads {landed} - the fix landed, so the entry has done its job. Delete "
        "exactly those keys from _PENDING_SIBLING_FIXES in "
        "tests/test_ui_surface_coverage.py and nothing else: every field still listed "
        "there is unread and still owed. Each deleted field is guarded by the ordinary "
        "rule from here on, and when a task loses its last entry, drop it from "
        "_OPEN_WAVE_TASKS too (test_every_open_wave_still_has_a_pending_entry says so). "
        "When the dict empties, _WAVE_LANDING_READS goes with it - it describes this "
        "wave's reads and nothing else."
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


# -- name collisions across views, pinned --------------------------------
#
# `is_consumed` sees a name, not a type, so two views that publish the same
# field name are indistinguishable to it. That is a harmless lean towards
# false negatives on the ordinary rule, and a *dangerous* one on the two
# self-clearing lists, which react to a read by telling a human to delete an
# entry. `U2 (cost surfaces)` produced exactly that collision for real: it
# adds `ModeOptionView.notice` and reads it in `ui/render/ribbon.js` as
# `m.notice`, which by name alone clears the `UnreadableModeView.notice`
# entry - on a view the browser cannot reach until something reads
# `ActionRow.unreadable_modes`, the field that carries it. Reachability is
# what separates the two, and it has to hold on *both* lists: the three
# `unreadable_modes` keys sit in `_PENDING_SIBLING_FIXES` (U2 wires them in
# `ui/menus.js`), where a false clear would delete a promise rather than an
# exemption. `TargetSlot.player_candidates` is the allowlist's live instance
# of the same shape.


#: `ui/render/ribbon.js`'s mode chips: a read of a *different* view's
#: `notice` and `number`. Used to pin the collision rather than describe it.
_U2_MODE_NOTICE_READ = "const label = m.number + '. ' + m.text; const why = m.notice;"


#: The three keys `ui/menus.js` reaches only through `ActionRow.unreadable_modes`,
#: in the shape they hold in `_PENDING_SIBLING_FIXES`. Written out rather than
#: read off the live dict for the reason the source below is written out: a pin
#: that leans on where an entry happens to sit today stops pinning the rule the
#: day it moves.
_UNREADABLE_MODES_PENDING = {
    ("ActionRow", "unreadable_modes"): _Pending(
        "U2 (cost surfaces)", "the row's unreadable-mode list"
    ),
    ("UnreadableModeView", "mode_number"): _Pending(
        "U2 (cost surfaces)", "which mode the entry names"
    ),
    ("UnreadableModeView", "notice"): _Pending(
        "U2 (cost surfaces)", "the engine's own sentence for it"
    ),
}


def test_a_collision_on_an_unreachable_view_does_not_clear_a_promise() -> None:
    """The merge-time false clear, pinned on the entries it actually hit.
    `UnreadableModeView` hangs off `ActionRow.unreadable_modes`, so a browser
    that does not read *that* is not reading this view's `notice`, whatever
    the name says - and the ribbon's own `m.notice` must not report the
    promise landed while the link is still unread.

    The source here is the read and nothing else, deliberately: a pin built
    on top of the live `ui/` asserts that today's `ui/` happens not to read
    the link, which stops being true the day somebody wires it and turns a
    rule pin into a merge conflict. This pins the rule."""
    landed = landed_pending_entries(_U2_MODE_NOTICE_READ, pending=_UNREADABLE_MODES_PENDING)
    assert landed == []


def test_reading_the_row_list_does_clear_the_whole_promise() -> None:
    """The other side of it, which is what actually happened: `ui/menus.js`
    reads the link and both fields behind it, so all three entries clear
    together. A rule that only ever refuses to clear would be a list that
    never empties."""
    source = (
        "for (const mode of (row && row.unreadable_modes) || []) "
        "byNumber.set(mode.mode_number, mode);\n"
        "const why = modes.map((mode) => mode.notice).join('\\n');"
    )
    landed = landed_pending_entries(source, pending=_UNREADABLE_MODES_PENDING)
    assert landed == sorted(_UNREADABLE_MODES_PENDING)


def test_the_same_collision_does_not_clear_another_allowlist_entry() -> None:
    """The same exposure one surface over: `TargetSlot.player_candidates`
    sits under the allowlisted `Action.target_slots`, so a read of
    `player_candidates` on the affordances' own slot view must not delete
    its entry."""
    source = "const players = slot.player_candidates || [];"
    assert ("TargetSlot", "player_candidates") not in wired_allowlist_entries(source)


#: A pending entry on a view behind an unread link, and one on a view the
#: browser holds, sharing a field name - the pending list's half of the same
#: collision. `Action.land_face` rides the row; `CardFaceView` hangs off
#: `_CardViewFields.faces`.
_LAND_FACE_PENDING = {
    ("Action", "land_face"): _Pending("U1 (cast path correctness)", "the row's MDFC land face"),
    ("CardFaceView", "land_face"): _Pending(
        "U1 (cast path correctness)", "the face list's copy of it"
    ),
    ("_CardViewFields", "faces"): _Pending("U1 (cast path correctness)", "CR 712's face list"),
}


def test_a_collision_on_an_unreachable_view_does_not_clear_a_pending_entry() -> None:
    """Reading the name on the row clears the row's entry and leaves the face
    list's alone, because nothing read the link that carries the face list."""
    landed = landed_pending_entries("if (row.land_face) playLand(row);", pending=_LAND_FACE_PENDING)
    assert ("Action", "land_face") in landed
    assert ("CardFaceView", "land_face") not in landed
    assert ("_CardViewFields", "faces") not in landed


def test_a_pending_entry_still_clears_once_the_link_and_the_field_are_read() -> None:
    """The self-clearing property is the reason the pending list is allowed to
    exist, so reachability must not quietly cost it. Read the link *and* the
    field and every entry is reported landed, exactly as before."""
    landed = landed_pending_entries(
        "const faces = card.faces || [];\nfaces.forEach((f) => f.land_face && offer(f));",
        pending=_LAND_FACE_PENDING,
    )
    assert landed == sorted(_LAND_FACE_PENDING)


def test_a_bare_word_is_not_a_read() -> None:
    """`Array.prototype.splice` must not launder `Action.splice_options`, and
    a bare identifier is not a property read."""
    assert not is_consumed("splice_options", "next.splice(index, 0, id);")
    assert not is_consumed("improvise", "const improvise = 1;")


# -- a wave that finishes may not leave fields parked ---------------------


def test_an_entry_parked_on_a_closed_wave_is_an_orphan() -> None:
    pending = {
        ("ActionRow", "x_costs"): _Pending("U2 (cost surfaces)", "a b c d e f"),
        ("ActionRow", "improvise"): _Pending("U9 (still going)", "a b c d e f"),
    }
    assert orphaned_pending_entries(pending, ("U9 (still going)",)) == [("ActionRow", "x_costs")]


def test_an_open_wave_with_nothing_pending_on_it_is_reported() -> None:
    pending = {("ActionRow", "x_costs"): _Pending("U2 (cost surfaces)", "a b c d e f")}
    assert waves_with_nothing_pending(pending, ("U2 (cost surfaces)", "U9 (gone)")) == ["U9 (gone)"]


#: One task parking two fields, for the two halves of the rule below. The
#: sources these are measured against are written out in full rather than
#: appended to the live `ui/`, for the reason the collision pins are: a pin
#: that leans on what today's `ui/` happens not to read stops pinning the
#: rule the day somebody wires the field.
_ONE_TASK_TWO_FIELDS = {
    ("ActionRow", "improvise"): _Pending("U2 (cost surfaces)", "the row's artifacts to tap"),
    ("ActionRow", "x_costs"): _Pending("U2 (cost surfaces)", "the row's charge per X"),
}


def test_a_wave_that_landed_one_field_and_parked_another_is_reported() -> None:
    """The audit's finding, mechanised: `U2` wired `improvise` and left
    `x_costs` unread, which says the task finished rather than that it is
    still in flight."""
    source = "const tappable = row.improvise || [];"
    assert abandoned_pending_entries(_ONE_TASK_TWO_FIELDS, source) == [("ActionRow", "x_costs")]


def test_a_wave_with_nothing_landed_yet_is_still_in_flight() -> None:
    """The other half, and the reason the rule is per task rather than per
    field: while none of a task's fields is read the task has not shipped,
    and its entries are exactly what the pending list is for."""
    assert abandoned_pending_entries(_ONE_TASK_TWO_FIELDS, "const x = 1;") == []


def test_every_pending_entry_names_a_wave_that_is_still_open() -> None:
    orphans = orphaned_pending_entries(_PENDING_SIBLING_FIXES, _OPEN_WAVE_TASKS)
    assert not orphans, (
        f"these entries are parked on a task that is no longer open: {orphans} - the task "
        "finished without wiring the field, so it is not pending on anybody. Wire it up in "
        "ui/, or move it to _NOT_CONSUMED_ON_PURPOSE with a reason kind."
    )


def test_every_open_wave_still_has_a_pending_entry() -> None:
    dead = waves_with_nothing_pending(_PENDING_SIBLING_FIXES, _OPEN_WAVE_TASKS)
    assert not dead, (
        f"_OPEN_WAVE_TASKS declares {dead} open and no entry is pending on it - delete it "
        "from _OPEN_WAVE_TASKS so a later entry cannot park itself on a task nobody is "
        "running any more."
    )


def test_no_open_wave_has_finished_leaving_fields_parked(ui_source: str) -> None:
    abandoned = abandoned_pending_entries(_PENDING_SIBLING_FIXES, ui_source)
    assert not abandoned, (
        f"these fields are parked on a task that has already wired up others: {abandoned} - "
        "the task is done, so nothing is coming for these. Wire them up, or move them to "
        "_NOT_CONSUMED_ON_PURPOSE with a reason kind."
    )


# -- clause (d): a field the brief called a bug, mechanised ---------------
#
# The round this file got wrong a second time. Clause (d) of the brief names
# six fields and says of them, in those words, that they "must NOT be
# allowlisted - they are bugs, not exemptions". Two of them ended up in
# `_NOT_CONSUMED_ON_PURPOSE` anyway - `Action.x_range` as a `known-gap` whose
# own text conceded the live defect ("an X the engine will refuse can still
# be announced and comes back refused"), and `ActionRow.optional_costs` as
# `superseded` by the cast hints' own enumeration. Both reasons were true
# sentences about the browser; neither was a thing the brief left open.
#
# Prose cannot hold that line, because the whole failure mode is a later
# reader finding a defensible sentence for a field and writing it down. The
# two tests below hold it instead: a named field may not appear in the
# exemption list at all, and it must be either read by `ui/` or still owed by
# an open task. The only way to satisfy both is to wire it up.


#: The field names clause (d) called bugs, by the bare name it named them
#: under - a name rather than a `(class, field)` key on purpose, so an
#: engine bump that publishes `x_range` on some *other* view inherits the
#: same refusal instead of arriving unclaimed.
#:
#: `faces`, `mode_number` and the rest are not here: the brief did not name
#: them, and they are the links and neighbours its six ride on. They are
#: held by `_PENDING_SIBLING_FIXES` and the rules above it like any other
#: parked field.
_NAMED_AS_BUGS_BY_THE_BRIEF: frozenset[str] = frozenset(
    {
        "land_face",
        "x_range",
        "spell_face",
        "improvise",
        "unreadable_modes",
        "optional_costs",
    }
)


def test_a_field_the_brief_named_as_a_bug_is_never_an_exemption() -> None:
    excused = sorted(
        f"{owner}.{name} ({_NOT_CONSUMED_ON_PURPOSE[owner, name].kind})"
        for owner, name in _NOT_CONSUMED_ON_PURPOSE
        if name in _NAMED_AS_BUGS_BY_THE_BRIEF
    )
    assert not excused, (
        f"{excused} sit in _NOT_CONSUMED_ON_PURPOSE, and clause (d) of the brief names "
        "those fields as bugs rather than exemptions. A true sentence about what the "
        "browser does instead is not a licence to excuse one: wire it up in ui/, or park "
        "it in _PENDING_SIBLING_FIXES on a task that is actually going to."
    )


def test_every_field_the_brief_named_as_a_bug_is_read_or_still_owed(ui_source: str) -> None:
    """The other half, and the one that cannot be satisfied by moving an entry
    between two dicts: a named field is read by `ui/`, or it is parked on an
    open task that has wired none of its own fields yet. Anything else is the
    field quietly going unread with nobody owing it."""
    reached = _reached_index(ui_source)
    stranded = sorted(
        f"{field.owner}.{field.name} (published on: {', '.join(field.surfaces)})"
        for field in published_fields()
        if field.name in _NAMED_AS_BUGS_BY_THE_BRIEF
        and not reached[field.owner, field.name]
        and (field.owner, field.name) not in _PENDING_SIBLING_FIXES
    )
    assert not stranded, (
        f"{stranded} are fields clause (d) named as bugs, and nothing under ui/ reads "
        "them and no open task is parked on them. Wire them up - an exemption is not "
        "available for these."
    )
