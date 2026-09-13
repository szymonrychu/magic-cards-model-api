"""One place that decides what a caller may see: hidden-information filtering.

Every endpoint that can return a card identity resolves the caller's `uid`
through `resolve_viewer` and passes the result through `redact_view` (state)
or `redact_log_entry` (the audit log) before it leaves the process. This is
the server-side replacement for what used to be a client-side filter in
mcp.py - the client only ever saw the *correct* answer here because it chose
to compute it; anything that talked to the API directly got the full state.
Now the server is the only place the rule lives.

`resolve_viewer` answers one of three ways:

- a player's username - the seat this uid opens, or, for an advisor uid
  (`repository.Advisor`), the human seat that advisor watches. Its own hand
  is visible, the opponent's is not, and neither library nor sideboard is -
  not even the viewer's own, matching the real game (nobody reads their own
  library). **An advisor resolving to a username, rather than to a fourth
  kind of viewer, is the whole design**: every function below then filters
  for the human without knowing an advisor exists, so an advisor's view is
  byte-identical to its human's by construction rather than by two code
  paths agreeing. What an advisor may *do* is a different question, and one
  this module never asks - `game_access.py` answers it.

  **This function is for visibility only.** Nothing that decides *who is
  acting* may call it, precisely because of the resolution above: an
  advisor uid resolves to its human here, so "does this uid open the seat
  this request names" comes back `True` for a credential whose entire
  point is that it may not act. `app.decide`, `app._answer_declaration`
  and `app._pregame_seat` each asked it that question and each therefore
  waved an advisor through, leaving `game_access.py`'s gate as the only
  thing refusing. They call `game_access.seat_for_uid` now - the
  authorization twin, which never resolves an advisor - and that is what
  makes the sentence above ("one this module never asks") true rather than
  merely intended.
- `ADMIN` - the game's admin key (store.py), minted once at creation and
  handed only to whoever created the game. Full visibility, no redaction -
  the operator's own eye, the same trust level `audit.snapshot` already
  grants the log by design.
- `None` - no uid, an unknown one, or one minted for a different game. Sees
  nothing hidden. A caller that presents nothing must never see more than
  one that presents garbage, and neither may see more than one that presents
  someone else's seat.
"""

from __future__ import annotations

from typing import Any

from .audit import ADVICE_ACTION, PLAN_ACTION
from .store import player_for_advisor_uid, store


class _Admin:
    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return "ADMIN"


ADMIN = _Admin()

Viewer = str | _Admin | None


def resolve_viewer(game_id: str, uid: str | None) -> Viewer:
    """What `uid` may see in `game_id`: see the module docstring."""
    if not uid:
        return None
    if store.is_admin(game_id, uid):
        return ADMIN
    player = store.player_for_uid(game_id, uid)
    if player is not None:
        return player
    return player_for_advisor_uid(game_id, uid)


def redact_view(view: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
    """A full state view, as `viewer` may see it.

    Counts are untouched - `hand_count`, `library_count`, etc. live on each
    player's own block, not here, so they stay visible however this comes
    out. Card identities are what this function guards: the opponent's hand,
    the contents and order of every library and sideboard, and - new here -
    the candidate cards of a pending decision that is not this viewer's own.
    A graveyard or exile card's own identity stays public either way (CR
    400.2) - what is trimmed for a card that is not the viewer's own is just
    its `cast_options` affordance (`_redact_cast_options`), the same
    "own block only" treatment `_redact_player` already gives `decklist`.

    A *hand* card now carries `cast_options` too (evoke and the rest of the
    CR 118.9 keywords that are cast from hand - `serialize._cast_options_of`),
    and it needs no `_redact_cast_options` pass of its own: `hands` below
    replaces every list that is not the viewer's own with `[]`, card, field
    and all. CR 402.3 - "a player can't look at the cards in another player's
    hand" - is answered by dropping the card, which is strictly more than
    dropping one of its keys, and a viewer with no uid at all matches no
    player name and so reads every hand empty.
    """
    if viewer is ADMIN:
        return view
    # Built from the *unredacted* view, before `libraries`/`sideboards` are
    # popped below, so `redact_pending_decision` can still hand the deciding
    # player the identity of the exact cards their own pending decision names
    # (CR 701.20e: a look is shown to the specified player) even though those
    # zones are about to disappear from the response entirely. Every card on
    # the board is indexed, not just the viewer's - a decision can only ever
    # name cards from the deciding player's own zone, so looking up an
    # opponent's card here is never reachable, but building the map without
    # first guessing whose cards matter is simpler than trying to scope it.
    has_pending = view.get("pending_decision") is not None
    card_lookup = _card_lookup(view) if has_pending else None
    hidden_ids = _hidden_card_ids(view, viewer) if has_pending else None
    view = dict(view)
    view["hands"] = {
        name: (cards if name == viewer else []) for name, cards in view.get("hands", {}).items()
    }
    view["players"] = [_redact_player(player, viewer) for player in view.get("players", [])]
    if view.get("pregame") is not None:
        view["pregame"] = _redact_pregame(view["pregame"], viewer)
    if "exile" in view:
        view["exile"] = [
            _redact_cast_options(_redact_facedown_card(card, viewer), viewer)
            for card in view["exile"]
        ]
    if "exile_pile" in view:
        view["exile_pile"] = [
            _redact_cast_options(_redact_facedown_card(card, viewer), viewer)
            for card in view["exile_pile"]
        ]
    if "graveyards" in view:
        view["graveyards"] = {
            player: [_redact_cast_options(card, viewer) for card in cards]
            for player, cards in view["graveyards"].items()
        }
    if "play_permissions" in view:
        view["play_permissions"] = {
            player: [_redact_permission(entry, viewer) for entry in entries]
            for player, entries in view["play_permissions"].items()
        }
    view.pop("libraries", None)
    view.pop("sideboards", None)
    if "known_cards" in view:
        view["known_cards"] = _redact_known_cards(view["known_cards"], viewer)
    if view.get("pending_decision") is not None:
        view["pending_decision"] = redact_pending_decision(
            view["pending_decision"], viewer, card_lookup, hidden_ids
        )
    if viewer is not None:
        view["you"] = viewer
    return view


def _redact_player(player: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
    """One entry of `players` (`serialize._player_view`), as `viewer` may see it.

    Everything on that block is public - life, counters, the pool, and the
    *counts* of each hidden zone - except `decklist`, which is the raw list
    this seat was booted from. A seat knows its own: CR 100.2a's constructed
    play is "a way of playing in which each player creates their own deck ahead
    of time", and CR 401.1 makes that deck their library at the start of the
    game. Nothing makes it the opponent's knowledge, and CR 401.2 - "players
    can't look at or change the order of cards in a library" - is the whole
    reason `libraries` is stripped from this view two lines below; handing over
    the decklist would give back by name exactly what that strips by object.

    So the field survives only on the viewer's own block. A bystander with no
    uid (`viewer is None`) reads nobody's, matching every other hidden field
    here: a caller that presents nothing must never see more than one that
    presents someone else's seat.
    """
    if "decklist" not in player or player.get("username") == viewer:
        return player
    return {key: value for key, value in player.items() if key != "decklist"}


def _redact_pregame(pregame: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
    """The CR 103.5/CR 103.6 pregame block (`opening.pregame_view`), as
    `viewer` may see it.

    Who owes a declaration, how many mulligans each seat has taken, how many
    cards keeping would cost them, and whose CR 103.6 turn it is are all
    public - they are the visible half of a mulligan across a real table.
    `opening_hand_options` is not: it names cards sitting in a player's opening
    hand, and CR 402.3 says "a player can't look at the cards in another
    player's hand". Each seat keeps its own list and gets an empty one for
    everybody else, the same shape `hands` uses.
    """
    options = pregame.get("opening_hand_options")
    if not isinstance(options, dict):
        return pregame
    return {
        **pregame,
        "opening_hand_options": {
            name: (offers if name == viewer else []) for name, offers in options.items()
        },
    }


def _redact_facedown_card(card: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
    """A card in a shared, public zone (CR 400.2), as `viewer` may see it.

    Exile is public - everybody sees a pile exists and roughly what is in it -
    but CR 406.3 lets a card sit there face down (foretell, CR 702.143a), and
    CR 703's face-down rule is the same "no name, no text, no characteristics
    visible" whoever is looking, except the object's own controller and the
    admin, who chose (or are owed) the look. `face_up` is already on every
    card view (`serialize._card_view`); this is the one place that acts on it
    for a *viewer other than the card's own controller*.

    `exile_pile` (the marker/owner/turn-exiled fact, CR 406.4) is not hidden
    by this - "there is a foretold card in exile, owned by alice" is public;
    only *which* card it is stays secret.
    """
    if card.get("face_up", True) or viewer is ADMIN:
        return card
    owner = card.get("controller") or card.get("owner")
    if viewer is not None and owner == viewer:
        return card
    return {
        "id": card.get("id"),
        "zone": card.get("zone"),
        "controller": card.get("controller"),
        "owner": card.get("owner"),
        "tapped": card.get("tapped"),
        "face_up": False,
        "name": "a face-down card",
        "card_name": None,
        "exile_pile": card.get("exile_pile"),
    }


def _redact_cast_options(card: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
    """A graveyard/exile card's `cast_options` (`serialize._cast_options_of`),
    as `viewer` may see it.

    Graveyard and exile only. A hand card carries the same field, and is
    covered by `redact_view` emptying `hands` wholesale rather than by this
    function - see that docstring.

    Graveyard and exile are public zones (CR 400.2) - the card underneath
    this field is never hidden, and this function does not touch any of the
    rest of the card's view. What it hides is narrower: `cast_options` is the
    *affordance* of casting this specific card ("you, specifically, may cast
    this with Flashback for {1}{W}{U} right now"), and CR 601.2b's keyword
    permissions on a card sitting off the battlefield are permissions to a
    *player* - `_cast_options_of` always builds them for the card's own
    controller-or-owner, because that is the only player who could ever hold
    one. So the field is kept for that player's own view, the same "own block
    only" treatment `_redact_player` already gives `decklist`, and dropped for
    everyone else - an opponent reading this card's row does not need a
    ready-made list of what its owner could do with it next.

    `None` when the field is not present at all (a card in a zone
    `_cast_options_of` never adds it to, or one with no live option) - the
    ordinary case, left untouched."""
    if "cast_options" not in card or viewer is ADMIN:
        return card
    owner = card.get("controller") or card.get("owner")
    if viewer is not None and owner == viewer:
        return card
    return {key: value for key, value in card.items() if key != "cast_options"}


def _redact_permission(entry: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
    """One `play_permissions` entry (`serialize._play_permission_view`), as
    `viewer` may see it. Every permission this engine grants today names a
    card that is already public (a flashback grant's graveyard card, a card
    exiled face up and made playable "this turn"), so there is nothing to
    hide in practice - but the entry nests a full card
    view under `"card"`, the same shape `exile` carries, so a hypothetical
    future permission over a face-down card is still covered the same way
    `_redact_facedown_card` already covers `exile`, rather than assuming this
    stays true forever."""
    card = entry.get("card")
    if not isinstance(card, dict):
        return entry
    return {**entry, "card": _redact_facedown_card(card, viewer)}


def _redact_known_cards(known: Any, viewer: Viewer) -> dict[str, Any]:
    """`serialize._known_cards_view`, as `viewer` may see it: its own entry
    only, the same "own block only" cut `hands` already gets.

    What one player knows about a hidden card is that player's alone to read
    back - an opponent's `known_cards` entry could name a card nobody has
    shown the viewer (a hand card seen only by an effect of the knower's own),
    so a viewer never reads another seat's entry, and a viewer who is nobody
    reads none. Fails closed: rebuilt only from the shape `serialize.
    _known_cards_view` actually produces - `hand`/`sideboard` lists of card
    views, `library` records carrying an integer `index_from_top` alongside a
    card view - and anything else is dropped rather than passed through."""
    if not isinstance(known, dict) or not isinstance(viewer, str):
        return {}
    block = known.get(viewer)
    if not isinstance(block, dict):
        return {}

    def cards(key: str) -> list[dict[str, Any]]:
        entries = block.get(key)
        if not isinstance(entries, list):
            return []
        return [card for card in entries if isinstance(card, dict) and card.get("id")]

    library: list[dict[str, Any]] = []
    for record in block.get("library") or ():
        if not isinstance(record, dict):
            continue
        index = record.get("index_from_top")
        card = record.get("card")
        if type(index) is not int or not isinstance(card, dict) or not card.get("id"):
            continue
        library.append({"index_from_top": index, "card": card})
    return {viewer: {"hand": cards("hand"), "library": library, "sideboard": cards("sideboard")}}


def _card_lookup(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every *object* a pending decision can name, by id, from the
    *unredacted* view.

    Used only to resolve the specific ids a pending decision names for the
    player it names them to - never exposed wholesale. Includes hidden zones
    (hands, libraries, sideboards) precisely because those are the zones a
    `SCRY`/`SURVEIL`/library-sourced `CHOOSE_CARDS` decision draws its
    candidates from.

    The stack is walked alongside them, and it is the one zone here that is
    not a list of cards. CR 113.1c: "an ability can be an activated or
    triggered ability on the stack. This kind of ability is an object", and
    CR 603.3 puts a triggered ability there "as an object that's not a card" -
    so an id a decision names can be an `AbilityOnStack.id`, which no card
    index can ever match. `CHOOSE_TARGETS` already offers one (CR 115.2 lets a
    spell or ability target an ability on the stack, and `decisions.
    _target_view` carries it as a `card`-kinded ref for exactly that reason),
    and `ORDER_TRIGGERS` (CR 603.3b) names nothing else. Without this, every
    such id resolved to nothing and the deciding seat was shown bare hex.

    Nothing is widened by including it: CR 400.2 lists the stack among the
    public zones - "public zones are zones in which all players can see the
    cards' faces ... graveyard, battlefield, stack, exile, ante, and command
    are public zones" - so a stack object's identity is already visible to
    everyone at the table, unlike the hands and libraries above it. The entry
    is `serialize._stack_view`'s own, so an ability arrives carrying its
    source's printed name and the ability's own text, which is what tells two
    triggers of the *same* permanent apart.
    """
    lookup: dict[str, dict[str, Any]] = {}
    for zone_key in ("hands", "libraries", "sideboards", "graveyards"):
        for cards in view.get(zone_key, {}).values():
            for card in cards:
                lookup[card["id"]] = card
    for card in view.get("battlefield", []):
        lookup[card["id"]] = card
    for obj in view.get("stack", []) or ():
        object_id = obj.get("id")
        if object_id is not None:
            lookup[object_id] = obj
    return lookup


def _hidden_card_ids(view: dict[str, Any], viewer: Viewer) -> set[str]:
    """The ids of every card in a zone `viewer` cannot see, from the
    *unredacted* view: every library and sideboard, and every hand but the
    viewer's own (CR 400.2 makes the library and the hand hidden zones; CR
    402.3: "a player can't look at the cards in another player's hand")."""
    hidden: set[str] = set()
    for zone_key in ("libraries", "sideboards"):
        for cards in view.get(zone_key, {}).values():
            hidden.update(card["id"] for card in cards)
    for name, cards in view.get("hands", {}).items():
        if name != viewer:
            hidden.update(card["id"] for card in cards)
    return hidden


def redact_pending_decision(
    pending: dict[str, Any],
    viewer: Viewer,
    card_lookup: dict[str, dict[str, Any]] | None = None,
    hidden_ids: set[str] | None = None,
) -> dict[str, Any]:
    """A pending decision, as `viewer` may see it.

    CR 101.4a lets a choice made in a hidden zone stay face down - "those
    cards may remain face down as they're chosen" - while still requiring the
    chooser to "clearly indicate which... they chose", i.e. that a choice is
    happening at all is not secret, only the identity of what was looked at.
    So a seat that is not the one deciding gets `{player, kind, prompt}` and
    nothing that names a card; the deciding player (and the admin, handled
    above) sees the rest - the candidates and how many to choose.

    For the deciding player, `cards` stays a bare list of ids (existing
    callers - `decide`'s own `moved`/`card_ids` - answer by id, and changing
    that shape would break them), but `card_details` is added alongside it:
    the full view of exactly those ids, resolved from `card_lookup`. Without
    this, a `scry`/`surveil` decision (CR 701.22a, CR 701.25a - a *look*,
    shown only to the deciding player, CR 701.20e) or a library-sourced
    `choose_cards` left the deciding player nothing to read the ids against,
    because `libraries` is stripped for every non-admin viewer, including
    that viewer's own. `card_details` is scoped to only the ids this decision
    actually names - never the rest of the zone they came from - so this
    grants exactly the visibility the effect itself grants, no more.

    An id that names an object on the *stack* rather than a card resolves
    here too (see `_card_lookup`), so a decision whose candidates are
    abilities - `ORDER_TRIGGERS` (CR 603.3b), a `CHOOSE_TARGETS` aimed at an
    ability (CR 115.2) - is answered against source names and ability text
    rather than against bare ids. An id that resolves to nothing is left out,
    which is what an `ORDER_TRIGGERS` batch still looks like today: CR 603.3b
    is asked *before* CR 603.3's "its controller puts it on the stack" has
    happened, so those abilities are in no zone yet and nothing here can find
    them. Clients fall back to the prompt's own numbering, which
    `machines/decisions.ask_trigger_order` writes positionally against
    `cards`.

    `source_card_id` goes only where the rest of the block goes, and even
    the deciding player gets `None` in its place when the asking object is
    in one of `hidden_ids` - a zone that viewer cannot see - so the id never
    points at a hidden card.
    """
    if viewer == pending.get("player"):
        if hidden_ids and pending.get("source_card_id") in hidden_ids:
            pending = {**pending, "source_card_id": None}
        cards = pending.get("cards") or ()
        if cards and card_lookup is not None:
            details = [card_lookup[card_id] for card_id in cards if card_id in card_lookup]
            if details:
                pending = {**pending, "card_details": details}
        return pending
    # `{player, kind, prompt}` and nothing else, `generation` included: only
    # the deciding seat can answer, so only the deciding seat has any use
    # for the token that says which question it is answering. Handing it to
    # everyone would widen this payload for no one's benefit.
    return {
        "player": pending.get("player"),
        "kind": pending.get("kind"),
        "prompt": pending.get("prompt"),
    }


def redact_paused_pending(
    pending: dict[str, Any], view: dict[str, Any], viewer: Viewer
) -> dict[str, Any]:
    """A pending decision an acting route hands straight back in its own
    paused response, as `viewer` - that route's actor - may see it.

    The same rule `redact_view` applies to the state read's copy: the rest
    of the block only for the deciding seat, and `source_card_id` nulled when
    the asking object sits in a zone `viewer` cannot see. `view` is the
    *unredacted* state view of the committed game, the board a state read
    would redact this same question against. No `card_details` here: these
    responses never carried it, and the state read is where it lives."""
    if viewer is ADMIN:
        return pending
    return redact_pending_decision(pending, viewer, hidden_ids=_hidden_card_ids(view, viewer))


def redact_snapshot(snapshot: dict[str, Any], viewer: str) -> dict[str, Any]:
    """One audit-log snapshot (`serialize.snapshot`), as a real seat may see
    it. Snapshots carry hands as plain name lists, never libraries, so only
    the hand needs blanking - to a same-length list of placeholders, so a
    reader can still count what it cannot name."""
    snapshot = dict(snapshot)
    snapshot["hands"] = {
        name: (names if name == viewer else ["<hidden>"] * len(names))
        for name, names in snapshot.get("hands", {}).items()
    }
    return snapshot


_HIDDEN_ZONES = {"hand", "library", "sideboard"}

#: Every field across the 19 notice shapes that carries a card's own printed
#: words rather than a bare number or id - `ModeNotice.mode_number` and this
#: module's own `zone`/`controller` bookkeeping are deliberately not here,
#: since neither names a card. `InterveningIfNotice.clause`,
#: `CastConditionNotice.condition`, and `OpeningHandNotice.card_name`/`.text`
#: are the three that are not plainly `text` - see each notice's own model in
#: the engine (`machines/triggers.py`, `machines/opening_hand.py`).
_NOTICE_TEXT_FIELDS = ("text", "clause", "condition", "card_name")


def redact_notices(notices: list[dict[str, Any]], viewer: Viewer) -> list[dict[str, Any]]:
    """`commands.run_command`'s new-notice records, as `viewer` may read
    them - one entry at a time, since a single command can raise notices
    about several different sources at once, each with its own zone and
    controller.

    Mirrors `redact_log_entry`'s own zone/owner guard just below
    (`_HIDDEN_ZONES`, `player`/`controller`) rather than sharing it directly:
    that guard runs only `isinstance(viewer, str)`, deliberately treating an
    anonymous read as the debugger's own full eye (see `app.game_log`'s own
    docstring) - correct for a snapshot nobody is hiding, wrong for a notice,
    which can name a card in a hand nobody asked to have named. Every viewer
    but `ADMIN` gets the same treatment here, anonymous included.

    `zone`/`controller` are read off the notice itself, not looked up here:
    `commands._new_notices` is the one place in the whole pipeline that still
    holds a live `Game` next to a freshly-appended notice, and this function
    - called again, later, against a notice already pulled back out of
    storage for a `GET .../log` read - has no `Game` left to ask. A
    `controller` of `None` (an unresolved source, or `OpeningHandNotice`'s
    own shape, which carries none at all) never counts as "this viewer's
    own", so a notice this function cannot positively clear is blanked for
    everyone but `ADMIN` - the same fail-closed posture the engine's own
    notice lists keep for a clause they could not read.
    """
    if viewer is ADMIN:
        return notices
    redacted: list[dict[str, Any]] = []
    for notice in notices:
        zone = notice.get("zone")
        controller = notice.get("controller")
        owns = controller is not None and controller == viewer
        if zone in _HIDDEN_ZONES and not owns:
            blanked = dict(notice)
            blanked["source_id"] = None
            blanked["source_name"] = "<hidden>"
            for field_name in _NOTICE_TEXT_FIELDS:
                if field_name in blanked:
                    blanked[field_name] = "<hidden>"
            redacted.append(blanked)
        else:
            redacted.append(notice)
    return redacted


def redact_log_entry(entry: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
    """One log entry, as `viewer` may read it.

    Two guards, and they do not have the same audience:

    - the snapshot is filtered the way state is, and the card name blanked
      when the action itself put a card into (or read it out of) a hidden
      zone that is not the viewer's own. This is a *seat's* view, so it is
      applied only for a real seat uid - a `uid`-less read stays the
      debugger's full eye `audit.py` documents its snapshots for, exactly as
      it always was.
    - `legal_actions_before` is dropped unless the seat it was enumerated
      for (`legal_actions_for`, written by `audit.record` from
      `records.LegalActionsBefore`, never from anything the request body
      carried) is this viewer. This one applies to *every* non-admin
      viewer, a `uid`-less read included.

    The second guard is what makes this the same door `GET .../legal-actions`
    (T11) is: that endpoint refuses any caller who does not present the named
    player's own uid or the admin key, because the answer names cards in that
    player's hand. `legal_actions_before` is the exact same answer, written
    into the log instead of returned live. A caller who presents nothing must
    therefore see no more of it than one who presents garbage - and less than
    one who presents a real seat uid. The admin key is the single exemption,
    matching `legal-actions`'s own; `app.py`'s `GET .../log` skips this whole
    function only for `ADMIN`.

    Fails closed: an entry carrying actions with no `legal_actions_for` at
    all - a row written before this pairing existed, or by some future
    caller that forgets it - is redacted for every seat rather than shown to
    all of them.

    The `zone`/`to_zone` card-blanking below only catches actions that record
    one of those keys alongside the card (`add_card`, `move_card`) - see the
    report for what is not covered.

    **A seat's own words are its own.** Two more guards, on the same "every
    non-admin viewer, `uid`-less included" footing as `legal_actions_before`:

    - `reasoning` - the acting seat's stated why - is dropped for anyone but
      the seat that wrote it. An opponent reading it live is reading the
      agent's mind mid-game, which is worth strictly more than seeing its
      hand.
    - a `plan_declared` row (`audit.PLAN_ACTION`) keeps only its `player`.
      Its detail is a whole turn's intent *and a prediction about the
      opponent*, declared before anything happened - the single most
      damaging thing in this ledger to hand the other seat. The row itself
      stays visible, because "bob declared a plan at this point" is public
      the way "bob took a long time" is; its content does not.
    - an `advice` row (`audit.ADVICE_ACTION`) keeps only its `player`, for
      exactly the same reason and by the same rule. A proposal names cards in
      the advised player's own hand, the targets it would pick and the mana it
      would spend, plus the advisor's reasoning for all of it - a plan
      declaration with the specifics filled in. Its author is that `player`:
      an advisor uid resolves to its human's username (`resolve_viewer`), so
      the human and the buddy advising it both read the row whole and nobody
      else reads any of it. "alice was given advice at this point" stays
      public, the way a plan declaration's own existence does.

    Both are keyed on `detail["player"]`, which is what the acting route
    recorded. That is safe *in this direction* and only in this direction:
    `detail["player"]` is caller-supplied on some routes, so trusting it
    could at worst let a caller hide its own words from itself, never reveal
    somebody else's - a request's `reasoning` is the reasoning that same
    request carried. Contrast `legal_actions_before`, which is server-computed
    hidden information about another seat and therefore must be keyed on
    `legal_actions_for` (see `records.LegalActionsBefore`).

    The post-game review (`review.py`) shows all of this, both seats, in
    full - that is what a review is for, and `app.py`'s `game_review`
    refuses to run at all until the game is over or ended.
    """
    entry = dict(entry)
    detail_player = (entry.get("detail") or {}).get("player")
    # Fails closed, like `legal_actions_before` below: `viewer` has to be a
    # real seat *and* be this row's own actor. A `uid`-less read is
    # `None`, and a row nobody made carries no player - "None is None" must
    # not read as "this is your row".
    own = viewer is ADMIN or (isinstance(viewer, str) and detail_player == viewer)
    if not own:
        entry["reasoning"] = None
        if entry.get("action") in (PLAN_ACTION, ADVICE_ACTION):
            entry["detail"] = {"player": detail_player} if detail_player else {}
    detail_notices = (entry.get("detail") or {}).get("notices")
    if detail_notices:
        entry["detail"] = {**entry["detail"], "notices": redact_notices(detail_notices, viewer)}
    if isinstance(viewer, str):  # a real seat - not admin, not "nobody"
        if entry.get("snapshot"):
            entry["snapshot"] = redact_snapshot(entry["snapshot"], viewer)
        detail = entry.get("detail") or {}
        zone = detail.get("zone") or detail.get("to_zone")
        owner = detail.get("player") or detail.get("controller")
        if zone in _HIDDEN_ZONES and owner != viewer and "card" in detail:
            entry["detail"] = {**detail, "card": "<hidden>"}
    if entry.get("legal_actions_before") is not None and entry.get("legal_actions_for") != viewer:
        entry["legal_actions_before"] = None
        entry["legal_actions_for"] = None
    return entry
