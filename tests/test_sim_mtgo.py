"""MTGO game log parser (T-123 S6): one test per catalogue kind plus the accounting rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from magic_cards_model_api.sim import mtgo
from magic_cards_model_api.sim.mtgo import Event, Unmapped, parse_mtgo_log, strip_markup

FIXTURES = Path(__file__).parent / "fixtures" / "mtgo"
ALL_FIXTURES = sorted(FIXTURES.glob("*.txt"))
ALL_FIXTURES = [p for p in ALL_FIXTURES if p.name != "README.txt"]

PREFIX = "A joined the game.\nB joined the game.\nTurn 1: A\n"


def one(line: str, prefix: str = PREFIX) -> Event:
    """Parse `line` after a two-player prefix and return the event it became."""
    _, events, unmapped = parse_mtgo_log(prefix + line + "\n")
    assert not unmapped, unmapped
    return events[-1]


# (kind, line, player, card, targets, amount)
KIND_CASES = [
    ("roll", "A rolled a 5.", "A", None, [], 5),
    ("join", "B joined the game.", "B", None, [], None),
    ("play_first", "A chooses to play first.", "A", None, [], None),
    ("play_first", "B chooses to not play first.", "B", None, [], None),
    ("mulligan", "B mulligans to six cards.", "B", None, [], 6),
    ("keep", "A begins the game with seven cards in hand.", "A", None, [], 7),
    (
        "keep",
        "B puts a card on the bottom of their library and begins the game with six cards in hand.",
        "B",
        None,
        [],
        6,
    ),
    ("skip_draw", "A skips their draw step.", "A", None, [], None),
    ("draw", "B draws a card.", "B", None, [], 1),
    ("draw", "A draws a card with [Opt].", "A", "Opt", [], 1),
    ("draw", "A draws a card with @[Clue@:1,2:@] token's ability.", "A", "Clue", [], 1),
    ("land", "A plays @[Mountain@:129800,601:@].", "A", "Mountain", [], None),
    (
        "cast",
        "A casts @[Monastery Swiftspear@:126282,602:@].",
        "A",
        "Monastery Swiftspear",
        [],
        None,
    ),
    ("cast", "A casts [Burst Lightning] targeting B.", "A", "Burst Lightning", ["B"], None),
    ("cast", "A casts [Stonecoil Serpent]. (X is 2).", "A", "Stonecoil Serpent", [], 2),
    (
        "cast",
        "A casts @[Fatal Push@:1,2:@] targeting @[Monastery Swiftspear@:3,4:@].",
        "A",
        "Fatal Push",
        ["Monastery Swiftspear"],
        None,
    ),
    (
        "activate",
        "A activates an ability of [Walking Ballista] ( Walking Ballista deals 1 damage to any "
        "target.) targeting B.",
        "A",
        "Walking Ballista",
        ["B"],
        None,
    ),
    (
        "activate",
        "A activates an ability of @[Clue@:1,2:@] token (Draw a card.).",
        "A",
        "Clue",
        [],
        None,
    ),
    (
        "trigger",
        "A puts triggered ability from @[Monastery Swiftspear@:1,2:@] onto the stack (Prowess "
        "@i(Whenever you cast a noncreature spell.)@i).",
        "A",
        "Monastery Swiftspear",
        [],
        None,
    ),
    (
        "trigger",
        "A puts a triggered ability from [Bloodtithe Harvester] onto the stack (When this "
        "enters, target creature gets -1/-1, targeting is fun.) targeting B.",
        "A",
        "Bloodtithe Harvester",
        ["B"],
        None,
    ),
    (
        "attack",
        "B is being attacked by @[Monastery Swiftspear@:1,2:@].",
        "B",
        None,
        ["Monastery Swiftspear"],
        None,
    ),
    (
        "attack",
        "B 's @[Liliana of the Veil@:1,2:@] is being attacked by [Tarmogoyf] and [Goyf Two]",
        "B",
        "Liliana of the Veil",
        ["Tarmogoyf", "Goyf Two"],
        None,
    ),
    (
        "block",
        "[Bloodtithe Harvester] blocks [Stonecoil Serpent].",
        None,
        "Bloodtithe Harvester",
        ["Stonecoil Serpent"],
        None,
    ),
    (
        "damage_prevented",
        "3 damage caused by [Bloodtithe Harvester] to [Stonecoil Serpent] is prevented.",
        None,
        "Bloodtithe Harvester",
        ["Stonecoil Serpent"],
        3,
    ),
    ("discard", "B discards @[Lightning Bolt@:1,2:@].", "B", "Lightning Bolt", [], None),
    (
        "put_onto_battlefield",
        "A puts [Goblin Token] onto the battlefield attacking B",
        "A",
        "Goblin Token",
        ["B"],
        None,
    ),
    ("put_onto_battlefield", "A puts [Elf] onto the battlefield.", "A", "Elf", [], None),
    (
        "token",
        "B's [Bloodtithe Harvester] creates a Blood Token.",
        "B",
        "Bloodtithe Harvester",
        ["Blood"],
        1,
    ),
    (
        "token",
        "A 's @[Clue Maker@:1,2:@] creates two Zombie tokens.",
        "A",
        "Clue Maker",
        ["Zombie"],
        2,
    ),
    ("sacrifice", "B sacrifices [Blood Token].", "B", "Blood Token", [], None),
    ("to_graveyard", "A puts [Island] into their graveyard.", "A", None, ["Island"], None),
    (
        "mill",
        "A mills [Island] and [Snapcaster Mage].",
        "A",
        None,
        ["Island", "Snapcaster Mage"],
        None,
    ),
    (
        "exile",
        "A exiles [Island], [Snapcaster Mage], and [Opt] with with [Murktide Regent].",
        "A",
        "Murktide Regent",
        ["Island", "Snapcaster Mage", "Opt"],
        None,
    ),
    (
        "bounce",
        "A returns [Stonecoil Serpent] to its owner's hand with [Fading Hope].",
        "A",
        "Fading Hope",
        ["Stonecoil Serpent"],
        None,
    ),
    (
        "counter_spell",
        "A counters [Kitesail Freebooter] with [Counterspell].",
        "A",
        "Counterspell",
        ["Kitesail Freebooter"],
        None,
    ),
    (
        "fizzle",
        "B's [Lightning Bolt] is removed from the stack because it has no legal targets.",
        "B",
        "Lightning Bolt",
        [],
        None,
    ),
    (
        "counters_added",
        "B puts a +1/+1 counter on [Walking Ballista].",
        "B",
        "Walking Ballista",
        [],
        1,
    ),
    (
        "counters_removed",
        "B removes a +1/+1 counter from [Walking Ballista].",
        "B",
        "Walking Ballista",
        [],
        1,
    ),
    (
        "reveal",
        "B reveals their hand to [Thoughtseize], containing [Mountain] and [Goblin Guide].",
        "B",
        "Thoughtseize",
        ["Mountain", "Goblin Guide"],
        None,
    ),
    ("concede", "B has conceded from the game.", "B", None, [], None),
    ("lose", "B loses the game.", "B", None, [], None),
    ("win", "A wins the game.", "A", None, [], None),
]


@pytest.mark.parametrize(("kind", "line", "player", "card", "targets", "amount"), KIND_CASES)
def test_kind(kind, line, player, card, targets, amount):
    # a join or roll after a turn header would start a second game, so these go first
    ev = one(line, prefix="" if kind in ("roll", "join") else PREFIX)
    assert ev.kind == kind
    assert ev.player == player
    assert ev.card == card
    assert ev.targets == targets
    assert ev.amount == amount
    assert ev.raw == line


def test_turn_kind_and_game_wide_turn_index():
    _, events, _ = parse_mtgo_log(
        "A joined the game.\nB joined the game.\nTurn 1: A\nTurn 1: B\nTurn 2: A\n"
    )
    turns = [e for e in events if e.kind == "turn"]
    assert [(e.player, e.turn) for e in turns] == [("A", 1), ("B", 2), ("A", 3)]


def test_every_catalogue_kind_has_a_case_and_a_table_entry():
    assert {c[0] for c in KIND_CASES} | {"turn"} == set(mtgo.KINDS)
    assert len(mtgo.KINDS) == 31


def test_events_carry_the_game_wide_turn():
    _, events, _ = parse_mtgo_log(
        "A joined the game.\nB joined the game.\nTurn 1: A\nA plays [Island].\n"
    )
    assert [e.turn for e in events] == [None, None, 1, 1]


def test_dat_markup_is_stripped():
    raw = (
        "@P@Pstone_owl joined the game.@PTurn 2: x "
        "@[Azusa, Lost but Seeking@:43070,541:@] @i(t)@i\x01"
    )
    assert strip_markup(raw) == "stone_owl joined the game.Turn 2: x Azusa, Lost but Seeking (t)"
    assert strip_markup("@Pa\x00b\x7fc\xe9") == "abc"
    players, events, _ = parse_mtgo_log(
        "@PA joined the game.\n@PB joined the game.\n@PTurn 1: A\n"
        "@PA casts @[Azusa, Lost but Seeking@:43070,541:@] targeting B.\n"
    )
    assert players == ["A", "B"]
    assert events[-1].card == "Azusa, Lost but Seeking"
    assert events[-1].targets == ["B"]


def test_unmapped_lines_are_listed_with_line_numbers():
    text = (FIXTURES / "rendered_chatter_names.txt").read_text()
    _, _, unmapped = parse_mtgo_log(text)
    by_no = {u.line_no: u for u in unmapped}
    assert sorted(by_no) == [1, 2, 3, 5, 6, 14, 23, 30, 52, 56, 74, 82, 83, 89, 90]
    assert all(isinstance(u, Unmapped) for u in unmapped)
    assert by_no[23].text == "[20:00:38] Old Man Hemlock scrys 1 (0 top, 1 bottom)."
    assert by_no[23].reason == "no mapping for this line"
    assert by_no[6].reason == by_no[14].reason == by_no[30].reason == "chat line"
    assert by_no[82].reason == by_no[83].reason == "continuation of line 81"
    assert by_no[3].reason == by_no[74].reason == "observer line"
    assert by_no[1].reason == "export header"
    assert by_no[52].reason == "game-system line"
    assert by_no[89].reason == "connection line"


def test_player_names_with_spaces_and_apostrophes():
    text = (FIXTURES / "rendered_chatter_names.txt").read_text()
    players, events, _ = parse_mtgo_log(text)
    assert players == ["Old Man Hemlock", "Dara O'Brien Jr"]
    token = next(e for e in events if e.kind == "token")
    assert token.player == "Dara O'Brien Jr"
    assert token.card == "Bloodtithe Harvester"
    fizzle = next(e for e in events if e.kind == "fizzle")
    assert fizzle.player == "Dara O'Brien Jr"
    players, events, _ = parse_mtgo_log((FIXTURES / "dat_two_games_spaced_names.txt").read_text())
    assert players == ["Mr. Grim", "Lotus Eater 5"] or players == ["Lotus Eater 5", "Mr. Grim"]
    attack = next(e for e in events if e.kind == "attack" and e.card == "Liliana of the Veil")
    assert attack.player == "Lotus Eater 5"
    assert attack.targets == ["Tarmogoyf"]


def test_multi_line_entry_folds_into_the_head_and_accounts_every_line():
    text = (FIXTURES / "rendered_chatter_names.txt").read_text()
    _, events, unmapped = parse_mtgo_log(text)
    head = next(e for e in events if e.line_no == 81)
    assert head.kind == "trigger"
    assert head.card == "Aether Channeler"
    assert head.targets == ["Goblin Token"]
    assert head.raw.count("\n") == 2
    assert {u.line_no for u in unmapped} >= {82, 83}


def test_timestamps_and_brackets_and_markup_give_the_same_event():
    a = one("[20:00:27] A casts [Burst Lightning] targeting B.")
    b = one("@PA casts @[Burst Lightning@:1,2:@] targeting B.")
    c = one("A casts Burst Lightning targeting B.")
    assert (a.kind, a.player, a.card, a.targets) == (b.kind, b.player, b.card, b.targets)
    assert (a.kind, a.player, a.card, a.targets) == (c.kind, c.player, c.card, c.targets)


def test_chat_lines_are_unmapped_with_a_reason():
    _, events, unmapped = parse_mtgo_log(PREFIX + "[12:00:00] [CHAT] A: A casts [Opt].\n")
    assert len(events) == 3
    assert [(u.line_no, u.reason) for u in unmapped] == [(4, "chat line")]


def test_blank_lines_are_skipped_and_line_numbers_are_physical():
    _, events, unmapped = parse_mtgo_log(
        "A joined the game.\n\n   \nB joined the game.\nwhat is this\n"
    )
    assert [e.line_no for e in events] == [1, 4]
    assert [(u.line_no, u.reason) for u in unmapped] == [(5, "no mapping for this line")]


def test_no_damage_life_or_death_kinds_exist():
    assert not {"damage", "life", "life_change", "dies", "death", "destroy"} & set(mtgo.KINDS)
    ev = parse_mtgo_log(PREFIX + "B takes 3 damage.\nB dies.\nA gains 5 life.\n")[2]
    assert [u.reason for u in ev] == ["no mapping for this line"] * 3


def test_later_games_are_unmapped_and_only_the_first_game_is_parsed():
    text = (FIXTURES / "dat_two_games_spaced_names.txt").read_text()
    _, events, unmapped = parse_mtgo_log(text)
    later = [u for u in unmapped if u.reason.startswith("later game")]
    assert later
    assert later[0].reason == "later game in the same log; import one game at a time"
    second_join = min(u.line_no for u in later)
    assert second_join > 26
    assert all(e.line_no < second_join for e in events)
    assert {u.line_no for u in unmapped if u.line_no >= second_join} == set(
        range(second_join, len(text.splitlines()) + 1)
    )
    assert all(u.reason == later[0].reason for u in unmapped if u.line_no >= second_join)
    assert max(e.turn for e in events if e.turn is not None) >= 1


def test_raw_dat_text_is_split_on_the_p_framing():
    raw = (
        "$0A1B2C3D-0000-1111-2222-333344445555\x00\x01\x02"
        "\x05\x00@PA rolled a 5.\x01\x02\x03"
        "\x07@P@PB joined the game.\x00\x00"
        "\x09@P@PA joined the game.\x04"
        "\x02@PTurn 1: A\x08\x09"
        "\x0a@PA plays @[Mountain@:1,2:@].\x00"
        "\x0b@PA casts @[Opt@:3,4:@] targeting B.\x01\n\r"
    )
    players, events, unmapped = parse_mtgo_log(raw)
    assert sorted(players) == ["A", "B"]
    assert [e.kind for e in events] == ["roll", "join", "join", "turn", "land", "cast"]
    assert events[3].player == "A"
    assert events[-1].targets == ["B"]
    assert [(u.line_no, u.reason) for u in unmapped] == [(1, "dat header")]


@pytest.mark.parametrize("path", ALL_FIXTURES, ids=lambda p: p.name)
def test_every_line_is_accounted_for(path):
    text = path.read_text()
    non_empty = [i for i, ln in enumerate(text.splitlines(), 1) if ln.strip()]
    players, events, unmapped = parse_mtgo_log(text)
    assert players
    assert len(events) + len(unmapped) == len(non_empty)
    assert sorted([e.line_no for e in events] + [u.line_no for u in unmapped]) == non_empty


def test_fixture_event_shapes():
    _, events, unmapped = parse_mtgo_log((FIXTURES / "dat_short_game.txt").read_text())
    assert unmapped == []
    assert events[0].kind == "roll"
    assert events[-1].kind == "concede"
    kinds = {e.kind for e in events}
    assert {"mulligan", "keep", "skip_draw", "reveal", "discard", "trigger", "attack"} <= kinds
