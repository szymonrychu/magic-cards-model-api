"""A seat has exactly two priority verbs: `pass` and `pass_until`.

The rules position this file pins down, verified against the Comprehensive
Rules (MagicCompRules 20260819) in the session that wrote it:

- CR 117.1: "The player with priority may cast spells, activate abilities, and
  take special actions." That is the whole list, and passing (CR 117.3d, "If a
  player has priority and chooses not to take any actions, that player passes")
  is what a player does instead of one of them.
- CR 117.4: "If all players pass in succession ... the spell or ability on top
  of the stack resolves or, if the stack is empty, the phase or step ends." So
  resolving the top of the stack and ending a step are *consequences* of every
  player passing, not moves anybody makes.
- CR 703.2: "Turn-based actions are not controlled by any player", and CR 703.1
  has them happen automatically when a step or phase begins or ends.

Together those say that `resolve_top`, `advance` and `set_step` are not player
actions at all - they are engine consequences and harness affordances that were
exposed to seats as though they were siblings of `pass`. They are gone from
every seat-facing surface here. The HTTP routes behind them stay, because they
are how the *test harness* drives and observes a board (including CR 500.3's
untap and cleanup steps, which a game never rests in for a live seat -
`engine_api.settle_priorityless_steps` crosses them) - see the README.

A seat told about a verb that does not exist is the defect this file stands
against: `mcp.py`'s docstrings, `seat_cli`'s help, `src/mtg_agent/driver/
prompt.py` and `docs/ai_seat_brief.md` are the only instructions an agent seat
ever reads.
"""

from __future__ import annotations

import argparse
import inspect
from pathlib import Path
from typing import Any

from magic_cards_model_api import mcp as mcp_module
from magic_cards_model_api import seat_cli
from magic_cards_model_api.mcp import build_server
from magic_cards_model_api.seatkit import Seats
from mtg_agent.driver import prompt as prompt_module

REPO = Path(__file__).resolve().parents[1]

#: The two verbs a seat has, and the only two.
PASS_VERBS = ("pass", "pass_until")

#: Retired MCP tools, with the rule that says each was never a player action.
RETIRED_TOOLS: dict[str, str] = {
    "pass_priority": "split into `pass` and `pass_until` - one verb did two jobs.",
    "resolve_top": "CR 117.4: the top of the stack resolves because everyone passed.",
    "advance": "CR 703.2: turn-based actions are not controlled by any player.",
    "set_step": "no rule puts the turn marker anywhere; that is board setup.",
}

#: Retired `seat_cli` subcommands, same judgement.
RETIRED_SUBCOMMANDS = ("resolve", "advance", "set-step")


def a_server_only_introspected() -> Any:
    """A server whose tools are only ever read, never called - so the `Seats`
    behind it never has to reach an API. Same shape `test_front_end_parity.py`
    and `test_autopass.py` use."""
    seats = Seats.__new__(Seats)
    seats.api = "http://testserver"
    seats._pinned_game_id = "unused"
    return build_server(seats)


def tool_functions() -> dict[str, Any]:
    registry = a_server_only_introspected()._tool_manager._tools
    assert registry, "the MCP server registered no tools at all"
    return {name: entry.fn for name, entry in registry.items()}


def subparsers() -> argparse.Action:
    return next(a for a in seat_cli.build_parser()._actions if getattr(a, "choices", None))


def agent_facing_texts() -> dict[str, str]:
    """Every word an agent seat is given about what it may do, by source.

    The MCP module docstring and each tool's own docstring are what an MCP
    client's `tools/list` shows the model; `prompt.py` is the driver's system
    prompt; `ai_seat_brief.md` is what a `claude --print` seat is handed.
    """
    texts = {
        "mcp.py module docstring": mcp_module.__doc__ or "",
        "seat_cli.py module docstring": seat_cli.__doc__ or "",
        "src/mtg_agent/driver/prompt.py": Path(prompt_module.__file__).read_text(),
        "docs/ai_seat_brief.md": (REPO / "docs" / "ai_seat_brief.md").read_text(),
    }
    for name, fn in tool_functions().items():
        texts[f"the {name} tool docstring"] = inspect.getdoc(fn) or ""
    sub = subparsers()
    for name, parser in sub.choices.items():
        helps = [parser.description or "", *(a.help or "" for a in parser._actions)]
        texts[f"the `seat_cli {name}` help"] = "\n".join(helps)
    return texts


def test_the_mcp_server_offers_exactly_the_two_pass_verbs() -> None:
    names = set(tool_functions())
    for verb in PASS_VERBS:
        assert verb in names, f"the {verb} tool is missing - a seat cannot pass"
    gone = names & set(RETIRED_TOOLS)
    assert not gone, f"retired tools still registered: {sorted(gone)}"


def test_the_seat_cli_offers_exactly_the_two_pass_verbs() -> None:
    choices = set(subparsers().choices)
    assert "pass" in choices and "pass-until" in choices, sorted(choices)
    gone = choices & set(RETIRED_SUBCOMMANDS)
    assert not gone, f"retired subcommands still registered: {sorted(gone)}"


def test_the_plain_pass_takes_no_run_arguments() -> None:
    """`pass` is the primitive - one priority, given up. Everything about a
    run (a destination, the stops, the `resume` token) belongs to `pass_until`,
    and a `pass` that still carried them is the over-grown surface again."""
    parameters = inspect.signature(tool_functions()["pass"]).parameters
    assert set(parameters) == {"player_uid", "reasoning"}, sorted(parameters)

    flags = {
        option
        for action in subparsers().choices["pass"]._actions
        for option in getattr(action, "option_strings", ())
    }
    assert flags <= {"-h", "--help"}, sorted(flags)


def test_no_seat_facing_text_names_a_verb_that_no_longer_exists() -> None:
    """The Waker of Waves defect: a tool description that names a verb the
    toolset no longer has teaches the seat to call something that cannot be
    called, and the seat has no other source to correct it from."""
    for source, text in agent_facing_texts().items():
        flat = " ".join(text.split())
        for verb, why in RETIRED_TOOLS.items():
            for mention in (f"`{verb}`", f"{verb}(", f"`{verb}(", f"{verb}()"):
                assert mention not in flat, f"{source} still names the retired `{verb}` - {why}"
        for command in RETIRED_SUBCOMMANDS:
            for mention in (f"`{command}`", f"seat_cli {command}", f"`{command} "):
                assert mention not in flat, (
                    f"{source} still names the retired `seat_cli {command}` subcommand"
                )


#: What each brief has to teach, in that brief's own spelling: the MCP driver
#: reads tool names, the `claude --print` seat reads CLI subcommands.
TAUGHT: dict[str, tuple[str, ...]] = {
    "src/mtg_agent/driver/prompt.py": ("`pass`", "pass_until", "stop_on_stack"),
    "docs/ai_seat_brief.md": ("`pass`", "pass-until", "no-stop-on-stack"),
}


def test_the_two_verbs_are_taught_everywhere_a_seat_reads() -> None:
    """The other half: a seat that is never told `pass_until` exists passes one
    priority at a time through every window it could never act in, and a seat
    never told about the stack stop does not know it has a way to be handed
    back a spell cast in a window it was passing through."""
    texts = agent_facing_texts()
    for source, expected in TAUGHT.items():
        flat = " ".join(texts[source].split())
        for token in expected:
            assert token in flat, f"{source} never mentions {token}"
