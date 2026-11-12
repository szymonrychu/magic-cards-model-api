"""SimLimits env parsing and the redaction / support helpers of the sim package."""

from __future__ import annotations

from typing import Any

import pytest

from magic_cards_model_api import decks, scryfall
from magic_cards_model_api.sim.config import SimLimits
from magic_cards_model_api.sim.redact import SUPPORT_NOTE, redact, support_block

SILENT_CARD_NAME = "Inscrutable Husk"
SILENT_CARD_RAW: dict[str, Any] = {
    "id": "ffffffff-0000-0000-0000-000000000001",
    "oracle_id": "ffffffff-0000-0000-0000-000000000002",
    "name": SILENT_CARD_NAME,
    "type_line": "Creature - Test Fixture",
    "mana_cost": "{2}{G}",
    "cmc": 3.0,
    "oracle_text": "Whenever this creature glimmers, frobnicate target zorblax.",
    "colors": ["G"],
    "color_identity": ["G"],
    "keywords": [],
    "power": "2",
    "toughness": "2",
    "layout": "normal",
    "legalities": {"standard": "legal", "pioneer": "legal", "modern": "legal"},
    "rarity": "common",
    "set": "tst",
}


def test_defaults_match_the_owner_answers():
    lim = SimLimits.from_env({})
    assert (lim.token, lim.max_sandboxes, lim.sandbox_idle_ttl_s, lim.call_timeout_s) == (
        None,
        1,
        1800,
        25,
    )
    assert (lim.max_match_games, lim.match_wall_s, lim.max_batches) == (10, 1500, 1)
    assert (lim.max_script_steps, lim.worker_max_rss_mb) == (200, 1024)
    assert (lim.max_claude_matches, lim.claude_deadline_s, lim.result_ttl_s) == (1, 14400, 86400)


def test_env_overrides_and_empty_token_is_none():
    lim = SimLimits.from_env({"MTG_SIM_MAX_MATCH_GAMES": "3", "MTG_SIM_TOKEN": ""})
    assert lim.max_match_games == 3 and lim.token is None


def test_token_is_read_when_set():
    assert SimLimits.from_env({"MTG_SIM_TOKEN": "placeholder-token"}).token == "placeholder-token"


def test_bad_number_raises_naming_the_variable():
    with pytest.raises(ValueError, match="MTG_SIM_CALL_TIMEOUT_S"):
        SimLimits.from_env({"MTG_SIM_CALL_TIMEOUT_S": "soon"})


def test_redact_drops_credential_keys_and_secret_strings():
    out = redact({"admin_key": "k", "x": [{"uid": "u"}, "see abc123"]}, secrets=["abc123"])
    assert out == {"x": [{}, "<redacted>"]}


def test_redact_drops_every_credential_key_at_depth():
    obj = {"a": {"seat_uids": [1], "key": "k", "advisor_uids": [2], "keep": 1}}
    assert redact(obj) == {"a": {"keep": 1}}


def test_support_block_notes_nothing_for_handled_cards():
    assert support_block(["Grizzly Bears"]) == {"support_warnings": []}


def test_support_block_notes_silent_cards(monkeypatch: pytest.MonkeyPatch):
    real_fetch_card = scryfall.fetch_card

    def fetch(name: str) -> dict[str, Any]:
        return SILENT_CARD_RAW if name == SILENT_CARD_NAME else real_fetch_card(name)

    monkeypatch.setattr(scryfall, "fetch_card", fetch)
    decks.clear_deck_support_cache()
    try:
        block = support_block([SILENT_CARD_NAME, SILENT_CARD_NAME])
    finally:
        decks.clear_deck_support_cache()
    assert [w["status"] for w in block["support_warnings"]] == ["silent"]
    assert block["support_note"] == SUPPORT_NOTE


def test_baseline_is_off_by_default_and_a_flag_turns_it_on():
    assert SimLimits.from_env({}).baseline_enabled is False
    assert SimLimits.from_env({"MTG_SIM_BASELINE_ENABLED": "1"}).baseline_enabled is True
    assert SimLimits.from_env({"MTG_SIM_BASELINE_ENABLED": "false"}).baseline_enabled is False
    with pytest.raises(ValueError, match="MTG_SIM_BASELINE_ENABLED"):
        SimLimits.from_env({"MTG_SIM_BASELINE_ENABLED": "maybe"})


def test_claude_seat_defaults_and_overrides():
    lim = SimLimits.from_env({})
    assert (lim.claude_model, lim.claude_effort) == ("claude-opus-5-5", "low")
    assert lim.claude_models == (
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-sonnet-5-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
    )
    lim = SimLimits.from_env(
        {
            "MTG_SIM_CLAUDE_MODEL": "claude-sonnet-5",
            "MTG_SIM_CLAUDE_EFFORT": "high",
            "MTG_SIM_CLAUDE_MODELS": "claude-sonnet-5, claude-opus-5",
        }
    )
    assert (lim.claude_model, lim.claude_effort) == ("claude-sonnet-5", "high")
    assert lim.claude_models == ("claude-sonnet-5", "claude-opus-5")


def test_max_snapshots_defaults_to_16_and_reads_the_env():
    assert SimLimits.from_env({}).max_snapshots == 16
    assert SimLimits.from_env({"MTG_SIM_MAX_SNAPSHOTS": "4"}).max_snapshots == 4
