"""The descriptions the seat MCP server serves, as the model actually reads them.

Claude Code truncates every MCP tool description (and the server's
instructions) at `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH`, so a tool whose
served description is its whole Python docstring loses everything past the
cut. `mcp_descriptions.TOOL_DESCRIPTIONS` is the served text instead, each
entry within `DESCRIPTION_CAP`, with guidance shared by many tools stated once
in `SERVER_INSTRUCTIONS`.

The phrase checks below carry over every assertion the docstring tests
(`test_front_end_parity.py`, `test_autopass.py`, `test_pass_surface.py`,
`test_prompt_matches_tools.py`, `test_cast_cost_choices.py`) make about what a
seat is taught, applied to the text that is now served.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from magic_cards_model_api import mcp_descriptions
from magic_cards_model_api.mcp import Seats, build_server
from magic_cards_model_api.mcp_descriptions import (
    DESCRIPTION_CAP,
    SERVER_INSTRUCTIONS,
    TOOL_DESCRIPTIONS,
)

SERVED_SURFACE_BUDGET = 46_500  # +1.4k: `baton: str` on 23 acting tools (T3)


def served_tools() -> list[Any]:
    server = build_server(Seats(api="http://testserver", game_id="unused"))
    return asyncio.run(server.list_tools())


def flat(text: str) -> str:
    return " ".join(text.split())


def described(name: str) -> str:
    return flat(TOOL_DESCRIPTIONS[name])


def instructions() -> str:
    return flat(SERVER_INSTRUCTIONS)


# -- shape --------------------------------------------------------------------


def test_every_registered_tool_has_exactly_one_description() -> None:
    names = {tool.name for tool in served_tools()}
    assert set(TOOL_DESCRIPTIONS) == names, (
        f"missing: {sorted(names - set(TOOL_DESCRIPTIONS))}, "
        f"extra: {sorted(set(TOOL_DESCRIPTIONS) - names)}"
    )


def test_every_description_fits_the_cap() -> None:
    assert DESCRIPTION_CAP == 4096
    over = {n: len(d) for n, d in TOOL_DESCRIPTIONS.items() if len(d) > DESCRIPTION_CAP}
    assert not over, over
    assert len(SERVER_INSTRUCTIONS) <= DESCRIPTION_CAP, len(SERVER_INSTRUCTIONS)


def test_the_text_is_plain_ascii() -> None:
    for name, text in [*TOOL_DESCRIPTIONS.items(), ("SERVER_INSTRUCTIONS", SERVER_INSTRUCTIONS)]:
        bad = sorted({ch for ch in text if ord(ch) > 127})
        assert not bad, f"{name} carries non-ASCII characters: {bad!r}"


def test_the_served_surface_is_smaller() -> None:
    total = sum(
        len(tool.description or "") + len(json.dumps(tool.input_schema)) for tool in served_tools()
    )
    assert total <= SERVED_SURFACE_BUDGET, total


def test_the_server_serves_these_descriptions() -> None:
    for tool in served_tools():
        assert tool.description == TOOL_DESCRIPTIONS[tool.name], (
            f"{tool.name} is not served from TOOL_DESCRIPTIONS"
        )


def test_every_parameter_is_explained() -> None:
    shared = instructions()
    for tool in served_tools():
        text = described(tool.name)
        for parameter in tool.input_schema.get("properties", {}):
            assert (
                f"`{parameter}`" in text or f"`{parameter}=" in text or (f"`{parameter}`" in shared)
            ), f"{tool.name}'s `{parameter}` is never explained"


# -- shared guidance, stated once --------------------------------------------


def test_the_instructions_carry_the_shared_rules() -> None:
    text = instructions()
    for phrase in (
        "`player_uid`",
        "`reasoning`",
        "`dry_run`",
        "`mana_sources`",
        "`hold_priority`",
        "input_required",
        "`available_actions`",
        "`recent_log`",
        "`not_now`",
        "`max_seconds`",
        "`deciding_for`",
        "`decisions_made_by`",
        "CR 723",
        "`auto_answered`",
        "auto-answered by the seat harness",
        "`then_pass`",
        "compact JSON",
    ):
        assert phrase in text, f"SERVER_INSTRUCTIONS never says {phrase!r}"


def test_the_instructions_do_not_promise_a_uniform_release() -> None:
    """`test_the_mcp_module_docstring_does_not_promise_a_uniform_release`."""
    text = instructions()
    for tool in ("play_land", "suspend", "plot", "foretell", "cycle"):
        assert tool in text, f"{tool} holds priority by default and the instructions never say so"
    assert "hold priority by default" in text
    assert "CR 116.3" in text
    assert "CR 605.3b" in text, "the mana-ability hold is not described either"


# -- hold defaults ------------------------------------------------------------


HOLDS_BY_DEFAULT = {
    "play_land",
    "suspend",
    "plot",
    "foretell",
    "cycle",
    "turn_face_up",
    "bring_companion",
    "unlock_door",
}


def test_every_hold_by_default_tool_says_so_in_capitals() -> None:
    holding = set()
    for tool in served_tools():
        default = tool.input_schema.get("properties", {}).get("hold_priority", {}).get("default")
        if default is True:
            holding.add(tool.name)
    assert holding == HOLDS_BY_DEFAULT, holding
    for name, text in TOOL_DESCRIPTIONS.items():
        if name in HOLDS_BY_DEFAULT:
            assert "HOLDS PRIORITY BY DEFAULT" in text, name
        else:
            assert "HOLDS PRIORITY BY DEFAULT" not in text, name


def test_cycling_is_not_described_as_a_special_action() -> None:
    text = described("cycle")
    assert "CR 702.29a" in text
    assert "CR 117.3c" in text
    assert "not a special action" in text


def test_a_mana_ability_always_holds_and_decide_never_passes() -> None:
    assert "ALWAYS HOLDS PRIORITY" in described("activate")
    assert "CR 605.3b" in described("activate")
    assert "NEVER PASSES PRIORITY" in described("decide")
    assert "held_priority" in described("decide")


def test_then_pass_is_documented_where_it_exists() -> None:
    for name in ("cast", "activate", "decide", "play_land"):
        assert "`then_pass" in described(name), name
    text = instructions()
    assert "`first`" in text, "where the action's own outcome goes is never said"


# -- decide: a look-then-split ------------------------------------------------


THE_OLD_HARDCODED_PAIR = (
    "to the bottom of the library for a scry",
    "to the graveyard for a surveil",
    "Every card not named stays on top",
)

THE_OLD_DISCARD_ONLY_TEXT = (
    "the card id(s) to discard, exactly as many",
    "comma-separated card ids to discard/choose",
)


def test_decide_sends_a_seat_to_the_labels_for_the_destinations() -> None:
    text = TOOL_DESCRIPTIONS["decide"]
    assert "moved_label" in text and "kept_label" in text
    for lie in THE_OLD_HARDCODED_PAIR:
        assert lie not in text, lie
    assert "authoritative" in text
    assert "Devourer of Destiny" in text
    assert "exiled" in text


def test_decide_keeps_the_keyword_pair_only_as_the_fallback() -> None:
    text = described("decide")
    assert "CR 701.22a" in text and "CR 701.25a" in text
    fallback = text.index("Only when both labels are absent or empty")
    assert fallback < text.index("CR 701.22a")
    assert fallback < text.index("CR 701.25a")


def test_decide_states_the_count_for_a_split() -> None:
    text = TOOL_DESCRIPTIONS["decide"]
    section = text[text.index("`scry` and `surveil`") : text.index("`opening_hand_action`")]
    assert "pending_decision.exact" in section
    assert "pending_decision.amount" in section


def test_decide_makes_exact_the_switch_and_the_empty_answer_real() -> None:
    text = described("decide")
    assert "`exact`" in text
    assert "CR 701.23b" in text and "CR 701.23d" in text
    assert "CR 107.1c" in text
    assert "the empty answer is a real answer" in text
    for lie in THE_OLD_DISCARD_ONLY_TEXT:
        assert lie not in text, lie


def test_decide_teaches_a_search_as_one_choice_over_the_whole_zone() -> None:
    text = described("decide")
    assert "CR 701.23a" in text
    assert "not a walk" in text
    assert "every legal candidate" in text
    assert "card_labels" in text
    assert "CR 400.2" in text
    assert "default_cards" in text and "silence" in text
    assert "source` names the card asking" in text
    assert "Wooded Foothills" in text and "Breeding Pool" in text


def test_decide_tells_a_seat_to_paste_answer_with() -> None:
    text = described("decide")
    assert "answer_with" in text and "answer_field" in text
    assert "`generation`" in text


def test_decide_covers_every_kind() -> None:
    text = described("decide")
    for kind in (
        "choose_cards",
        "choose_targets",
        "may",
        "pay_life",
        "pay_energy",
        "pay_mana",
        "pay_energy_amount",
        "pay_life_amount",
        "choose_an_amount",
        "choose_a_quality",
        "choose_a_mode",
        "scry",
        "surveil",
        "opening_hand_action",
        "put_on_top_or_bottom",
        "put_into_hand_or_graveyard",
        "choose_pile",
        "choose_players",
        "order_triggers",
        "assign_combat_damage",
        "mulligan",
        "declare_attackers",
        "declare_blockers",
    ):
        assert f"`{kind}`" in text, f"decide never explains the `{kind}` kind"


# -- battlefield_state --------------------------------------------------------


SECTIONS = (
    "battlefield",
    "hands",
    "graveyards",
    "exile",
    "command",
    "stack",
    "combat",
    "players",
    "play_permissions",
    "pregame",
    "pending_decision",
    "recent_log",
    "known_cards",
    "library_contents",
    "sideboard_contents",
)


def test_battlefield_state_names_every_section() -> None:
    text = described("battlefield_state")
    for section in SECTIONS:
        assert f"`{section}`" in text, section
    assert "exile_pile" in text
    assert "omitted_sections" in text


def test_battlefield_state_tells_a_seat_the_label_and_search_fields_are_there() -> None:
    text = described("battlefield_state")
    assert "pending_decision" in text
    assert "moved_label" in text and "kept_label" in text
    assert "authoritative" in text
    assert "`exact` says whether" in text
    assert "card_labels" in text
    assert "default_cards" in text
    assert "CR 701.23b" in text and "CR 701.23d" in text


# -- pass / pass_until --------------------------------------------------------


RETIRED_TOOLS = ("pass_priority", "resolve_top", "advance", "set_step")
RETIRED_SUBCOMMANDS = ("resolve", "advance", "set-step")


def test_no_served_text_names_a_retired_verb() -> None:
    for source, text in [*TOOL_DESCRIPTIONS.items(), ("SERVER_INSTRUCTIONS", SERVER_INSTRUCTIONS)]:
        text = flat(text)
        for verb in RETIRED_TOOLS:
            for mention in (f"`{verb}`", f"{verb}(", f"`{verb}(", f"{verb}()"):
                assert mention not in text, f"{source} names the retired `{verb}`"
        for command in RETIRED_SUBCOMMANDS:
            for mention in (f"`{command}`", f"seat_cli {command}", f"`{command} "):
                assert mention not in text, f"{source} names the retired `{command}`"


def test_pass_points_at_pass_until_for_a_stretch() -> None:
    text = described("pass")
    assert "pass_until" in text
    for outcome in ("resolve_top", "end_step", "passed"):
        assert f"`{outcome}`" in text or f'"{outcome}"' in text, outcome


def test_pass_until_says_what_each_stop_costs() -> None:
    """`test_autopass.py::test_the_tool_docstring_says_what_each_stop_costs`."""
    text = described("pass_until")
    assert "CR 509.1" in text
    assert "block nothing and take the whole attack" in text
    assert "CR 508.1" in text
    assert "attack with nothing that turn" in text
    assert "I am not attacking this turn" in text
    assert "NEXT occurrence" in text
    assert "stopped_because" in text
    assert "`stop_on_stack=false`" in text
    for parameter in ("stop_when_attacked", "stop_when_you_can_attack"):
        assert f"`{parameter}=" in text and "true" in text, parameter
    assert "off by default" in text
    assert "represents a trick" in text
    assert "pass by hand" in text


def test_pass_until_hands_resume_back_and_runs_in_the_background() -> None:
    text = described("pass_until")
    assert "`resume`" in text and "verbatim" in text
    assert "`background`" in text
    assert 'for_turn="next"' in text
    assert "auto_passed" in text


def test_the_dsl_first_rule_is_served() -> None:
    for name in ("run_script", "stage_script", "list_scripts", "pass_until"):
        assert "DSL-first" in TOOL_DESCRIPTIONS[name], name
    assert "`repeat`" in described("run_script")


# -- cast ---------------------------------------------------------------------


def test_cast_teaches_the_pitch_cost() -> None:
    text = described("cast")
    assert "`choosing`" in text
    assert "Exile a white card from your hand" in text
    assert "`spell_face`" in text
    assert "`optional_costs`" in text


@pytest.mark.parametrize("name", sorted(("wait_for_priority", "pass_until", "run_script")))
def test_the_waiting_tools_explain_max_seconds(name: str) -> None:
    assert "`max_seconds`" in described(name)
    assert "sleep" in instructions() or "sleep" in described(name)


def test_the_module_exports_only_what_the_server_wires() -> None:
    assert {"DESCRIPTION_CAP", "TOOL_DESCRIPTIONS", "SERVER_INSTRUCTIONS"} <= set(
        vars(mcp_descriptions)
    )
