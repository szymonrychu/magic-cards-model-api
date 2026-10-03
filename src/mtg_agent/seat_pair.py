"""The seat pair: one orchestrator (the seat's own claude) driving two Opus
`seat-player` agents - `current` holds the baton and acts, `future` plans the
next decision read-only - over one of three channels."""

from __future__ import annotations

from enum import StrEnum

ROLE_CURRENT = "current"
ROLE_FUTURE = "future"

#: The one project-agent definition both roles share: `future` is promoted to
#: `current` by continuing the same agent, so there is a single definition.
SEAT_PLAYER = "seat-player"

#: The seat role (`MTG_ROLE`) that pairs; an advisor or buddy never does.
SEAT_ROLE = "seat"

#: Mirror of `magic_cards_model_api.baton.BATON_EXEMPT_TOOLS` (imported
#: nowhere here: that package pulls the whole game app in). A test pins the
#: two against each other.
BATON_EXEMPT_TOOLS = frozenset(
    {
        "battlefield_state",
        "game_log",
        "list_scripts",
        "report_problem",
        "pass_baton",
        "await_baton",
    }
)


class Channel(StrEnum):
    OFF = "off"
    TEAMS = "teams"
    SUBAGENTS = "subagents"

    @classmethod
    def parse(cls, raw: str) -> Channel:
        """`""` is the default (teams, the T1 probe verdict); anything outside
        the allowed values is a startup error naming them."""
        value = raw.strip().lower()
        if not value:
            return cls.TEAMS
        try:
            return cls(value)
        except ValueError:
            allowed = ", ".join(c.value for c in cls)
            raise ValueError(
                f"invalid SEAT_AGENT_CHANNEL {raw!r}: allowed values are {allowed}"
            ) from None


def current_name(n: int) -> str:
    return f"{ROLE_CURRENT}-{n}"


def future_name(n: int) -> str:
    return f"{ROLE_FUTURE}-{n}"


def pair_active(channel: Channel, role: str) -> bool:
    return role == SEAT_ROLE and channel is not Channel.OFF
