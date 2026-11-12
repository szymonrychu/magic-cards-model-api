"""Redaction of credentials from sim output, and the card-support note."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from magic_cards_model_api import decks

REDACTED = "<redacted>"
_CREDENTIAL_KEYS = frozenset({"admin_key", "seat_uids", "uid", "key", "advisor_uids"})

SUPPORT_NOTE = (
    "Some cards here are not fully supported by the engine: a 'silent' card resolves and does "
    "nothing. Treat any result that depends on them as unreliable and say so."
)


def redact(obj: Any, secrets: Iterable[str] = ()) -> Any:
    """Copy of `obj` without credential keys; strings containing a secret become "<redacted>"."""
    secret_list = [s for s in secrets if s]

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items() if k not in _CREDENTIAL_KEYS}
        if isinstance(value, list | tuple):
            return [walk(v) for v in value]
        if isinstance(value, str) and any(s in value for s in secret_list):
            return REDACTED
        return value

    return walk(obj)


def support_block(names: Iterable[str]) -> dict[str, Any]:
    """`support_warnings` for the named cards, plus `support_note` when there are any."""
    warnings = decks.deck_support_warnings(sorted(set(names)))
    block: dict[str, Any] = {"support_warnings": warnings}
    if warnings:
        block["support_note"] = SUPPORT_NOTE
    return block


_SUPPORT_NOTES = {
    "pending": "Card support could not be checked yet.",
    "unavailable": "Card support could not be checked at all.",
}


def support_unknown(state: str) -> dict[str, Any]:
    """The support block for a check that has not finished (`pending`) or failed (`unavailable`).

    An empty warning list alone would read as "all supported", so the status and a note say
    otherwise.
    """
    return {
        "support_status": state,
        "support_warnings": [],
        "support_note": _SUPPORT_NOTES[state]
        + " Do not treat these results as reliable until support is checked.",
    }
