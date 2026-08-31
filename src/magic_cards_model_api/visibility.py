"""One place that decides what a caller may see: hidden-information filtering.

Every endpoint that can return a card identity resolves the caller's `uid`
through `resolve_viewer` and passes the result through `redact_view` (state)
or `redact_log_entry` (the audit log) before it leaves the process. This is
the server-side replacement for what used to be a client-side filter in
mcp.py - the client only ever saw the *correct* answer here because it chose
to compute it; anything that talked to the API directly got the full state.
Now the server is the only place the rule lives.

`resolve_viewer` answers one of three ways:

- a player's username - the seat this uid opens. Its own hand is visible,
  the opponent's is not, and neither library nor sideboard is - not even
  the viewer's own, matching the real game (nobody reads their own library).
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

from .store import store


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
    return store.player_for_uid(game_id, uid)


def redact_view(view: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
    """A full state view, as `viewer` may see it.

    Counts are untouched - `hand_count`, `library_count`, etc. live on each
    player's own block, not here, so they stay visible however this comes
    out. Card identities are what this function guards: the opponent's hand,
    the contents and order of every library and sideboard, and - new here -
    the candidate cards of a pending decision that is not this viewer's own.
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
    card_lookup = _card_lookup(view) if view.get("pending_decision") is not None else None
    view = dict(view)
    view["hands"] = {
        name: (cards if name == viewer else []) for name, cards in view.get("hands", {}).items()
    }
    if "exile" in view:
        view["exile"] = [_redact_facedown_card(card, viewer) for card in view["exile"]]
    if "exile_pile" in view:
        view["exile_pile"] = [_redact_facedown_card(card, viewer) for card in view["exile_pile"]]
    if "play_permissions" in view:
        view["play_permissions"] = {
            player: [_redact_permission(entry, viewer) for entry in entries]
            for player, entries in view["play_permissions"].items()
        }
    view.pop("libraries", None)
    view.pop("sideboards", None)
    if view.get("pending_decision") is not None:
        view["pending_decision"] = redact_pending_decision(
            view["pending_decision"], viewer, card_lookup
        )
    if view.get("decision_timeouts"):
        view["decision_timeouts"] = [
            redact_decision_timeout(entry, viewer) for entry in view["decision_timeouts"]
        ]
    if viewer is not None:
        view["you"] = viewer
    return view


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


def _redact_permission(entry: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
    """One `play_permissions` entry (`serialize._play_permission_view`), as
    `viewer` may see it. Every permission this engine grants today names a
    card that is already public (cascade and suspend both exile face up), so
    there is nothing to hide in practice - but the entry nests a full card
    view under `"card"`, the same shape `exile` carries, so a hypothetical
    future permission over a face-down card is still covered the same way
    `_redact_facedown_card` already covers `exile`, rather than assuming this
    stays true forever."""
    card = entry.get("card")
    if not isinstance(card, dict):
        return entry
    return {**entry, "card": _redact_facedown_card(card, viewer)}


def _card_lookup(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every card on the board right now, by id, from the *unredacted* view.

    Used only to resolve the specific ids a pending decision names for the
    player it names them to - never exposed wholesale. Includes hidden zones
    (hands, libraries, sideboards) precisely because those are the zones a
    `SCRY`/`SURVEIL`/library-sourced `CHOOSE_CARDS` decision draws its
    candidates from.
    """
    lookup: dict[str, dict[str, Any]] = {}
    for zone_key in ("hands", "libraries", "sideboards", "graveyards"):
        for cards in view.get(zone_key, {}).values():
            for card in cards:
                lookup[card["id"]] = card
    for card in view.get("battlefield", []):
        lookup[card["id"]] = card
    return lookup


def redact_pending_decision(
    pending: dict[str, Any],
    viewer: Viewer,
    card_lookup: dict[str, dict[str, Any]] | None = None,
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
    """
    if viewer == pending.get("player"):
        cards = pending.get("cards") or ()
        if cards and card_lookup is not None:
            details = [card_lookup[card_id] for card_id in cards if card_id in card_lookup]
            if details:
                pending = {**pending, "card_details": details}
        return pending
    return {
        "player": pending.get("player"),
        "kind": pending.get("kind"),
        "prompt": pending.get("prompt"),
    }


def redact_decision_timeout(entry: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
    """One `decision_timeouts` entry (`store.record_decision_timeout`), as
    `viewer` may see it. `defaulted_to` can name cards from a hidden zone -
    a scry/surveil default leaves library cards it names in place, a
    `choose_cards` default (the cleanup discard) names a hand card before it
    is discarded - so this follows `redact_pending_decision`'s same CR 101.4a
    logic: the deciding player and the admin see which cards were defaulted
    on, everyone else sees only that a default happened, and to what kind of
    question, not what it named."""
    if viewer == entry.get("player"):
        return entry
    return {
        "player": entry.get("player"),
        "kind": entry.get("kind"),
        "prompt": entry.get("prompt"),
        "could_answer": entry.get("could_answer"),
        "rule": entry.get("rule"),
    }


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


def redact_log_entry(entry: dict[str, Any], viewer: str) -> dict[str, Any]:
    """One log entry, as a real seat may read it: its snapshot filtered the
    same way state is, and the card name blanked when the action itself put
    a card into (or read it out of) a hidden zone that is not viewer's own.

    This only catches actions that record a `zone`/`to_zone` alongside the
    card (`add_card`, `move_card`) - see the report for what is not covered.
    """
    entry = dict(entry)
    if entry.get("snapshot"):
        entry["snapshot"] = redact_snapshot(entry["snapshot"], viewer)
    detail = entry.get("detail") or {}
    zone = detail.get("zone") or detail.get("to_zone")
    owner = detail.get("player") or detail.get("controller")
    if zone in _HIDDEN_ZONES and owner != viewer and "card" in detail:
        entry["detail"] = {**detail, "card": "<hidden>"}
    return entry
