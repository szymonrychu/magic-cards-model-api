"""Seat-blind MTGO imports (T-156): a scenario seen from one player's seat never names an
opponent hand card before the log makes it public."""

from __future__ import annotations

from pathlib import Path

import pytest

from magic_cards_model_api.sim.mtgo import parse_mtgo_log
from magic_cards_model_api.sim.mtgo_convert import card_names, safe_player_names, to_scenario

FIXTURES = Path(__file__).parent / "fixtures" / "mtgo"
ALL_FIXTURES = [p for p in sorted(FIXTURES.glob("*.txt")) if p.name != "README.txt"]


def _types(players, events) -> dict[str, str]:
    lands = {"Forest", "Plains", "Island", "Swamp", "Mountain", "Blood Crypt", "Steam Vents"}
    return {
        n: "Basic Land" if n in lands else "Instant" if n in INSTANTS else "Creature"
        for n in card_names(players, events)
    }


INSTANTS = {
    "Swords to Plowshares", "Path to Exile", "Brave the Elements", "Lightning Bolt",
    "Fatal Push", "Thoughtseize", "Lava Spike", "Fading Hope", "Counterspell", "Opt",
    "Burst Lightning", "Thought Scour", "Consider", "Duress", "Kolaghan's Command",
}  # fmt: skip


def _convert(name: str, upto_turn: int | None, player: str | None):
    players, events, _ = parse_mtgo_log((FIXTURES / name).read_text())
    return to_scenario(
        players, events, upto_turn, type_lines=_types(players, events), player=player
    )


def _hand(scenario: dict, player: str) -> list[str]:
    return sorted(
        c["card"]
        for c in scenario["setup"]["cards"]
        if c["player"] == player and c.get("zone", "hand") == "hand"
    )


def _acts(scenario: dict) -> list[tuple]:
    return [
        (s["act"]["action"], s["act"].get("player"), s["act"].get("body", {}))
        for s in scenario["steps"]
        if "act" in s
    ]


def _notes(scenario: dict) -> str:
    return "\n".join(s["note"] for s in scenario["steps"] if "note" in s)


def test_opponent_hand_cards_of_the_turn_are_added_when_played_not_in_setup():
    scenario, unmapped = _convert("dat_combat_game.txt", 6, "Brightwater")
    assert [u.line_no for u in unmapped] == [16]  # the Clue token, as without a seat
    assert _hand(scenario, "kessig_wolf") == []
    acts = _acts(scenario)
    wolf = {"player": "kessig_wolf", "zone": "hand"}
    plains = ("add_card", None, {"card": "Plains", **wolf, "label": "kessig_wolf_Plains_3"})
    swords_label = "kessig_wolf_Swords_to_Plowshares_1"
    swords = ("add_card", None, {"card": "Swords to Plowshares", **wolf, "label": swords_label})
    assert acts[:3] == [
        plains,
        ("play_land", "kessig_wolf", {"card_id": "@kessig_wolf_Plains_3"}),
        swords,
    ]
    assert acts[3][0] == "cast" and acts[3][2]["card_id"] == "@kessig_wolf_Swords_to_Plowshares_1"
    assert "2 opponent hand cards" in _notes(scenario)

    # the same turn from the active player's own seat: their hand is theirs to see
    own, _ = _convert("dat_combat_game.txt", 6, "kessig_wolf")
    assert _hand(own, "kessig_wolf") == ["Plains", "Swords to Plowshares"]
    assert not [a for a in _acts(own) if a[0] == "add_card"]
    assert "0 opponent hand cards" in _notes(own)


def test_without_a_player_the_scenario_is_unchanged():
    seatless, _ = _convert("dat_combat_game.txt", 6, None)
    assert _hand(seatless, "kessig_wolf") == ["Plains", "Swords to Plowshares"]
    assert "opponent hand cards" not in _notes(seatless)


def test_cards_revealed_earlier_stay_in_the_opponent_hand():
    # Thoughtseize showed river_kite's Goblin Guide on turn 2; the Mountain of turn 5 is new
    scenario, _ = _convert("dat_short_game.txt", 5, "stone_owl")
    assert _hand(scenario, "river_kite") == ["Goblin Guide"]
    adds = [a for a in _acts(scenario) if a[0] == "add_card"]
    assert [a[2]["card"] for a in adds] == ["Mountain"]
    assert "1 opponent hand card " in _notes(scenario)
    own, _ = _convert("dat_short_game.txt", 5, "river_kite")
    assert _hand(own, "river_kite") == ["Goblin Guide", "Mountain"]


def test_an_opponent_instant_on_your_turn_appears_only_when_cast():
    players, events, _ = parse_mtgo_log((FIXTURES / "rendered_chatter_names.txt").read_text())
    names = safe_player_names(players)
    dara, hemlock = names["Dara O'Brien Jr"], names["Old Man Hemlock"]
    scenario, _ = to_scenario(
        players, events, 10, type_lines=_types(players, events), player="Dara O'Brien Jr"
    )
    assert "Fading Hope" not in _hand(scenario, hemlock)
    acts = _acts(scenario)
    i = next(i for i, a in enumerate(acts) if a[0] == "add_card")
    assert acts[i][2]["card"] == "Fading Hope"
    # added after Dara's Bolt and the active player's pass, right before Hemlock casts it
    assert acts[i - 1] == ("pass", dara, {})
    assert acts[i + 1][:2] == ("cast", hemlock)
    assert acts[i + 1][2]["card_id"] == "@" + acts[i][2]["label"]


def test_unknown_player_is_refused_with_the_names():
    with pytest.raises(ValueError, match="'river_kite', 'stone_owl'"):
        _convert("dat_short_game.txt", None, "nobody")


def _public_before(events, owner: str, line_no: int) -> set[str]:
    """Card names of `owner` the log showed before `line_no` (any public mention)."""
    seen: set[str] = set()
    for e in events:
        if e.line_no >= line_no:
            break
        if e.kind == "reveal" and e.player == owner:
            seen.update(e.targets)
        if e.kind == "bounce":
            seen.update(e.targets)
    return seen


@pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: p.name)
def test_no_opponent_hand_card_is_named_before_it_is_public(path):
    """Every turn, both seats: the opponent's setup hand holds only cards the log made public
    before the turn, and every other opponent hand card is added right before it is used."""
    players, events, _ = parse_mtgo_log(path.read_text())
    names = safe_player_names(players)
    types = _types(players, events)
    turns = sorted({e.turn for e in events if e.kind == "turn" and e.turn})
    for me in players:
        opp_log = next(p for p in players if p != me)
        opp = names[opp_log]
        for turn in turns:
            start = next(e.line_no for e in events if e.kind == "turn" and e.turn == turn)
            scenario, _ = to_scenario(players, events, turn, type_lines=types, player=me)
            public = _public_before(events, opp_log, start)
            hand = _hand(scenario, opp)
            assert set(hand) <= public, (me, turn, hand, public)
            setup_labels = {c["label"] for c in scenario["setup"]["cards"]}
            acts = _acts(scenario)
            for i, (action, who, body) in enumerate(acts):
                if action in ("cast", "play_land") and who == opp:
                    label = body["card_id"][1:]
                    if label in setup_labels:
                        continue
                    assert acts[i - 1][0] == "add_card", (me, turn, acts)
                    assert acts[i - 1][2]["label"] == label


@pytest.fixture
def sim():
    from magic_cards_model_api.sim.config import SimLimits
    from magic_cards_model_api.sim.manager import SandboxManager
    from magic_cards_model_api.sim.server import build_sim_server

    limits = SimLimits.from_env({})
    manager = SandboxManager(limits)
    yield build_sim_server(manager, limits)
    manager.shutdown()


def _call(server, name: str, **kw):
    import asyncio
    import json

    result = asyncio.run(server.call_tool(name, kw))
    return json.loads(str(result.content[0].text))


def test_import_tool_with_a_player_hides_and_then_plays_the_opponent_cards(sim):
    text = (FIXTURES / "dat_short_game.txt").read_text()
    out = _call(sim, "sim_import_mtgo_log", log_text=text, upto_turn=5, player="stone_owl",
                run=True)  # fmt: skip
    assert "error" not in out, out
    seat = out["perspective"]
    assert seat["player"] == "stone_owl" and seat["hidden_opponent_cards"] == 1
    assert "add_card" in seat["how"] and "log" in seat["how"]
    assert _hand(out["scenario"], "river_kite") == ["Goblin Guide"]
    run = out["run"]
    assert run["stopped_at"] is None, run["stop_reason"]
    assert all(s["ok"] for s in run["steps"]), run["steps"]
    assert "river_kite_Mountain_3" in run["labels"]

    seatless = _call(sim, "sim_import_mtgo_log", log_text=text, upto_turn=5)
    assert "perspective" not in seatless
    assert _hand(seatless["scenario"], "river_kite") == ["Goblin Guide", "Mountain"]


def test_import_tool_refuses_an_unknown_player_as_text(sim):
    text = (FIXTURES / "dat_short_game.txt").read_text()
    out = _call(sim, "sim_import_mtgo_log", log_text=text, player="nobody")
    assert "unknown player 'nobody'" in out["error"] and "'river_kite'" in out["error"]
