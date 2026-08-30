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
    view = dict(view)
    view["hands"] = {
        name: (cards if name == viewer else []) for name, cards in view.get("hands", {}).items()
    }
    view.pop("libraries", None)
    view.pop("sideboards", None)
    if view.get("pending_decision") is not None:
        view["pending_decision"] = redact_pending_decision(view["pending_decision"], viewer)
    if viewer is not None:
        view["you"] = viewer
    return view


def redact_pending_decision(pending: dict[str, Any], viewer: Viewer) -> dict[str, Any]:
    """A pending decision, as `viewer` may see it.

    CR 101.4a lets a choice made in a hidden zone stay face down - "those
    cards may remain face down as they're chosen" - while still requiring the
    chooser to "clearly indicate which... they chose", i.e. that a choice is
    happening at all is not secret, only the identity of what was looked at.
    So a seat that is not the one deciding gets `{player, kind, prompt}` and
    nothing that names a card; the deciding player (and the admin, handled
    above) sees the rest - the candidates and how many to choose.
    """
    if viewer == pending.get("player"):
        return pending
    return {
        "player": pending.get("player"),
        "kind": pending.get("kind"),
        "prompt": pending.get("prompt"),
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
