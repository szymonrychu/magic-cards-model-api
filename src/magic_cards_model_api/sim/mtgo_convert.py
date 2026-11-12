"""Convert a parsed MTGO game log into a sim scenario (T-123 S6).

A log hides hands and libraries, so a faithful action replay is impossible (see MEMORY).
Instead the board at the START of one turn is reconstructed by zone bookkeeping over the
events before it, and that turn's own actions become scenario steps:

- before the turn: lands and resolved permanent spells go to the battlefield, instants and
  sorceries to the graveyard, discards, sacrifices, mills and "into their graveyard" moves
  to the graveyard, exiles to exile, bounces to the owner's hand, counters onto the object;
- the turn itself: land, cast (targets as labels), activate, attack and block become `act`
  / `decide` steps, with the passes that resolve a spell or reach combat; what the engine
  does on its own (triggers, draws, tokens, reveals...) becomes a `note` step.

MTGO logs no damage, life changes or deaths (tests/fixtures/mtgo/README.txt section 6), so
life stays at 20 and creatures that died are still on the reconstructed battlefield: every
answer carries `BOARD_CAVEATS`. Events that cannot be expressed come back as `Unmapped`.
Pure code apart from `lookup_type_lines`, which reads card data through `scryfall.fetch_card`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from magic_cards_model_api.sim.mtgo import Event, Unmapped, parse_mtgo_log, strip_markup

BOARD_CAVEATS = (
    "MTGO logs record no deaths, no damage and no life changes. Creatures that died (or were"
    " removed by a spell whose effect the log does not show) may still be on the reconstructed"
    " battlefield, and life totals are guesses (20 unless a logged line changed them). Correct"
    " the board and life with the setup before trusting the result."
)
LOOKUP_WORKERS = 4
MAX_UNSUPPORTED = 200  # entries of `unsupported` echoed per answer; the rest are only counted
MAX_UNSUPPORTED_TEXT = 300  # chars of one unsupported line echoed back
_PERMANENT_TYPES = ("Artifact", "Battle", "Creature", "Enchantment", "Land", "Planeswalker")
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.:-]+")
_LABEL_CHARS = re.compile(r"[^A-Za-z0-9]+")
_COUNTER_TYPE = re.compile(r" (?:puts|removes) (?:a|an|\w+) (?P<ctype>.+?) counters? (?:on|from) ")
_BRACKETS = re.compile(r"\[([^\[\]]+)\]")
_TIMESTAMP = re.compile(r"^\[\d{1,2}:\d{2}:\d{2}\] ?")
_TOKEN = "tokens are not recreated: the sandbox holds real cards only; add the token by hand"
_PLAYER_COUNTERS = "counters on a player (poison and others) are not set by the import"
# Kinds that move nothing between public zones (or only between hidden ones).
_NO_ZONE_CHANGE = frozenset(
    {"roll", "join", "play_first", "mulligan", "keep", "turn", "skip_draw", "draw", "trigger",
     "activate", "attack", "block", "damage_prevented", "reveal", "concede", "lose", "win"}
)  # fmt: skip
# Kinds whose events carry card names in `targets`.
_CARD_TARGETS = frozenset(
    {"cast", "activate", "trigger", "attack", "block", "exile", "bounce", "mill",
     "to_graveyard", "counter_spell", "reveal"}
)  # fmt: skip


def _front_type_line(raw: dict[str, Any], name: str) -> str | None:
    """The type line of `name`'s face in a Scryfall object, or None when it is another card."""
    faces = raw.get("card_faces") or []
    names = [raw.get("name", ""), *(f.get("name", "") for f in faces)]
    if name.lower() not in {n.lower() for n in names if n}:
        return None  # a fuzzy match found some other card
    line = raw.get("type_line") or (faces[0].get("type_line") if faces else "") or ""
    return line.split(" // ")[0] or None


def _type_line(name: str) -> str | None:
    from magic_cards_model_api import scryfall

    return _front_type_line(scryfall.fetch_card(name), name)


def _front_oracle_text(raw: dict[str, Any], name: str) -> str | None:
    """The oracle text of `name`'s front face, or None when it is another card."""
    if _front_type_line(raw, name) is None:
        return None
    faces = raw.get("card_faces") or []
    return raw.get("oracle_text") or (faces[0].get("oracle_text") if faces else "") or ""


def _oracle_text(name: str) -> str | None:
    from magic_cards_model_api import scryfall

    return _front_oracle_text(scryfall.fetch_card(name), name)


def lookup_type_lines(names: Iterable[str], timeout_s: float) -> dict[str, str | None]:
    """Type line per card name through `scryfall.fetch_card`, never waiting over `timeout_s`.

    A name that fails, names another card, or is still being fetched at the deadline maps to
    None; the converter then reports the events that need it as Unmapped.
    """
    return _lookup(_type_line, names, timeout_s)


def lookup_oracle_texts(names: Iterable[str], timeout_s: float) -> dict[str, str | None]:
    """Front-face oracle text per card name, with the same bounds as `lookup_type_lines`."""
    return _lookup(_oracle_text, names, timeout_s)


def _lookup(
    fetch: Callable[[str], str | None], names: Iterable[str], timeout_s: float
) -> dict[str, str | None]:
    wanted = sorted(set(names))
    if not wanted:
        return {}
    pool = ThreadPoolExecutor(max_workers=LOOKUP_WORKERS, thread_name_prefix="mtgo-types")
    futures = {name: pool.submit(fetch, name) for name in wanted}
    wait(futures.values(), timeout=max(0.0, timeout_s))
    pool.shutdown(wait=False, cancel_futures=True)
    return {name: _result(future) for name, future in futures.items()}


def _result(future: Future[str | None]) -> str | None:
    if not future.done() or future.cancelled() or future.exception() is not None:
        return None
    return future.result()


_REMINDER = re.compile(r"\([^()]*\)")
_QUOTED = re.compile(r'"[^"]*"')
_ABILITY_LINE = re.compile(r"^(?P<cost>[^:]+):\s+(?P<effect>.+)$")
_LOGGED_ABILITY = re.compile(r" \((?P<text>[^()]*)\)\.?$")
_WORDS = re.compile(r"[^a-z0-9]+")


def _plain(text: str) -> str:
    return _WORDS.sub(" ", text.lower().replace("\u2212", "-")).strip()


def activated_effects(oracle_text: str) -> list[str]:
    """The effect text of each activated ability in an oracle text, in printed order.

    An activated ability is a line `<cost>: <effect>` (loyalty abilities `+1:` and `-2:`
    included); reminder text and quoted granted abilities are not abilities of the card.
    """
    effects = []
    for line in oracle_text.splitlines():
        line = _QUOTED.sub("", _REMINDER.sub("", line)).strip()
        hit = _ABILITY_LINE.match(line)
        if hit:
            effects.append(hit["effect"])
    return effects


def is_permanent(type_line: str) -> bool:
    return any(t in type_line for t in _PERMANENT_TYPES)


def card_names(players: list[str], events: list[Event]) -> set[str]:
    """Every card name the events mention, without player names and token names."""
    known = set(players)
    tokens = token_names(events)
    names: set[str] = set()
    for e in events:
        if e.card and e.kind != "token":
            names.add(e.card)
        if e.kind in _CARD_TARGETS:
            names.update(e.targets)
    return {
        n
        for n in names - known
        if not is_token(n, tokens) and not n.startswith("Triggered ability")
    }


def token_names(events: list[Event]) -> set[str]:
    """Names the log creates tokens under ("creates a Clue token" names "Clue")."""
    return {t for e in events if e.kind == "token" for t in e.targets}


def is_token(name: str, tokens: set[str]) -> bool:
    """Created as a token, or named like one ("[Goblin Token]" in the rendered form)."""
    return name in tokens or name.lower().endswith(" token")


def safe_player_names(players: list[str]) -> dict[str, str]:
    """Log name -> sandbox name (letters, digits and _ . : - only, unique)."""
    out: dict[str, str] = {}
    for player in players:
        base = _UNSAFE_NAME.sub("_", player).strip("_") or "player"
        if base in (".", ".."):
            base = "player"
        name, n = base, 2
        while name in out.values():
            name, n = f"{base}_{n}", n + 1
        out[player] = name
    return out


@dataclass
class _Obj:
    name: str
    owner: str
    zone: str
    label: str
    counters: dict[str, int] = field(default_factory=dict)


class _Board:
    def __init__(self) -> None:
        self.objs: list[_Obj] = []
        self._counts: dict[tuple[str, str], int] = {}

    def new(self, name: str, owner: str, zone: str) -> _Obj:
        n = self._counts.get((owner, name), 0) + 1
        self._counts[(owner, name)] = n
        label = f"{_LABEL_CHARS.sub('_', f'{owner}_{name}').strip('_')}_{n}"
        obj = _Obj(name, owner, zone, label)
        self.objs.append(obj)
        return obj

    def find(self, name: str, zones: tuple[str, ...], owner: str | None = None) -> list[_Obj]:
        return [
            o
            for o in self.objs
            if o.name == name and o.zone in zones and (owner is None or o.owner == owner)
        ]

    def take(self, name: str, owner: str, zones: tuple[str, ...]) -> _Obj | None:
        """The owner's first copy of `name` in the first of `zones` holding one."""
        for zone in zones:
            found = self.find(name, (zone,), owner)
            if found:
                return found[0]
        return None


class _Problem(Exception):
    """An event that cannot be expressed; the message is the Unmapped reason."""


@dataclass
class _Converter:
    names: dict[str, str]
    types: Mapping[str, str | None]
    tokens: set[str]
    oracle: Mapping[str, str | None] = field(default_factory=dict)
    board: _Board = field(default_factory=_Board)
    active: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    setup_adds: list[tuple[_Obj, str]] = field(default_factory=list)  # (object, setup zone)
    _block_step: dict[str, Any] | None = None

    # -- helpers ------------------------------------------------------------

    def player(self, name: str | None) -> str:
        if name is None or name not in self.names:
            raise _Problem(f"unknown player {name!r}")
        return self.names[name]

    def opponent(self, player: str) -> str:
        return next((p for p in self.names.values() if p != player), player)

    def type_of(self, card: str | None) -> str:
        if not card or is_token(card, self.tokens):
            raise _Problem(_TOKEN if card else "no card on this line")
        type_line = self.types.get(card)
        if not type_line:
            raise _Problem(f"unknown card {card!r}: no card data found (in time)")
        return type_line

    def resolve(
        self,
        name: str,
        owner: str | None = None,
        zones: tuple[str, ...] = ("battlefield", "graveyard", "exile"),
    ) -> _Obj:
        """One public object named `name`, searching `zones` in order.

        Copies under one player are interchangeable here (the log does not tell them apart),
        so the first is taken; copies under two players make the reference ambiguous.
        """
        if is_token(name, self.tokens):
            raise _Problem(f"{name} is a token; {_TOKEN}")
        for zone in zones:
            found = self.board.find(name, (zone,), owner)
            if found:
                if len({o.owner for o in found}) > 1:
                    raise _Problem(f"ambiguous target {name!r}: copies under more than one player")
                return found[0]
        where = f"{owner}'s" if owner else "any"
        raise _Problem(f"{name!r} is not in {where} {'/'.join(zones)} on the reconstructed board")

    def target(self, name: str) -> str:
        return self.names[name] if name in self.names else f"@{self.resolve(name).label}"

    def act(self, action: str, player: str | None, body: dict[str, Any] | None = None) -> None:
        step: dict[str, Any] = {"action": action}
        if player is not None:
            step["player"] = player
        if body:
            step["body"] = body
        self.steps.append({"act": step})

    def passes(self, first: str) -> None:
        self.act("pass", first)
        self.act("pass", self.opponent(first))

    def source_card(self, name: str, owner: str, zone: str = "hand") -> _Obj:
        """The owner's copy in `zone`, put there by the setup when the board holds none."""
        obj = self.board.take(name, owner, (zone,))
        if obj is None:
            obj = self.board.new(name, owner, zone)
            self.setup_adds.append((obj, zone))
        return obj

    def activated_index(self, e: Event) -> int | None:
        """The ability number to send for an activation: None keeps the default (0) for a
        card with at most one activated ability; a multi-ability card must match the logged
        ability text to one ability, else the line is Unmapped."""
        assert e.card is not None
        card = e.card
        oracle = self.oracle.get(card)
        if oracle is None:
            raise _Problem(
                f"cannot tell which ability of {card!r} was activated: no card data found (in time)"
            )
        effects = activated_effects(oracle)
        if len(effects) <= 1:
            return None
        logged = _LOGGED_ABILITY.search(_text(e))
        wanted = _plain(logged["text"]) if logged else ""
        hits = [i for i, fx in enumerate(effects) if wanted and _plain(fx) in wanted or
                wanted and wanted in _plain(fx)]  # fmt: skip
        if len(hits) != 1:
            raise _Problem(
                f"cannot tell which ability of {card!r} was activated: the logged text"
                f" {wanted!r} does not match exactly one of its {len(effects)} abilities"
            )
        return hits[0]

    # -- before the turn: zone bookkeeping ------------------------------------

    def replay(self, e: Event) -> None:
        kind = e.kind
        if kind in _NO_ZONE_CHANGE:
            return
        if kind == "token":
            raise _Problem(_TOKEN)
        player = self.player(e.player) if e.player is not None else None
        if kind in ("land", "cast", "put_onto_battlefield", "discard"):
            type_line = self.type_of(e.card)
            assert player is not None and e.card is not None
            permanent = kind != "discard" and (kind != "cast" or is_permanent(type_line))
            zone = "battlefield" if permanent else "graveyard"
            sources = ("hand",)  # a copy the board already knows there, e.g. a bounced one
            if kind == "put_onto_battlefield":
                sources = ("hand", "graveyard", "exile")
            elif kind == "cast":
                sources = (_cast_zone(e),)
            obj = self.board.take(e.card, player, sources)
            obj = obj or self.board.new(e.card, player, zone)
            obj.zone = zone
        elif kind == "sacrifice":
            assert player is not None and e.card is not None
            if is_token(e.card, self.tokens):
                raise _Problem(f"{e.card} is a token; {_TOKEN}")
            found = self.board.find(e.card, ("battlefield",), player)
            if not found:
                raise _Problem(f"{e.card!r} is not on {player}'s reconstructed battlefield")
            found[0].zone = "graveyard"
        elif kind in ("mill", "to_graveyard"):
            assert player is not None
            for name in e.targets:
                self.type_of(name)
            for name in e.targets:
                obj = self.board.take(name, player, ("hand",))
                (obj or self.board.new(name, player, "graveyard")).zone = "graveyard"
        elif kind in ("exile", "bounce"):
            zones = ("graveyard", "battlefield") if kind == "exile" else ("battlefield",)
            objs = [self.resolve(name, zones=zones) for name in e.targets]
            for obj in objs:
                obj.zone = "exile" if kind == "exile" else "hand"
        elif kind == "counter_spell":
            if not e.targets:
                raise _Problem("no countered spell on this line")
            name = e.targets[0]
            if is_permanent(self.type_of(name)):
                found = self.board.find(name, ("battlefield",))
                if not found:
                    raise _Problem(f"countered {name!r} is not on the reconstructed battlefield")
                found[-1].zone = "graveyard"
        elif kind == "fizzle":
            card = e.card or ""
            if is_permanent(self.types.get(card) or ""):
                found = self.board.find(card, ("battlefield",))
                if found:
                    found[-1].zone = "graveyard"
        elif kind in ("counters_added", "counters_removed"):
            if e.card in self.names:
                raise _Problem(_PLAYER_COUNTERS)
            match = _COUNTER_TYPE.search(_BRACKETS.sub(r"\1", strip_markup(e.raw)))
            if e.card is None or match is None:
                raise _Problem("cannot read the counter type or the object")
            obj = self.resolve(e.card, zones=("battlefield",))
            ctype, amount = match["ctype"], e.amount or 1
            sign = 1 if kind == "counters_added" else -1
            obj.counters[ctype] = max(0, obj.counters.get(ctype, 0) + sign * amount)
            if not obj.counters[ctype]:
                del obj.counters[ctype]
        else:
            raise _Problem(f"no conversion for kind {kind}")

    # -- the turn itself: steps -----------------------------------------------

    def emit(self, e: Event) -> None:
        kind = e.kind
        if kind != "block":
            self._block_step = None
        if kind == "turn":
            return
        if kind == "land":
            player = self.player(e.player)
            self.type_of(e.card)
            assert e.card is not None
            obj = self.source_card(e.card, player)
            self.act("play_land", player, {"card_id": f"@{obj.label}"})
            obj.zone = "battlefield"
        elif kind == "cast":
            player = self.player(e.player)
            type_line = self.type_of(e.card)
            assert e.card is not None
            targets = [self.target(t) for t in e.targets]
            zone = _cast_zone(e)
            obj = self.source_card(e.card, player, zone)
            body: dict[str, Any] = {"card_id": f"@{obj.label}"}
            if zone != "hand":
                body["from_zone"] = zone
            if targets:
                body["targets"] = targets
            if e.amount is not None:
                body["x_value"] = e.amount
            if player != self.active:
                self.act("pass", self.active)
            self.act("cast", player, body)
            self.passes(player)
            obj.zone = "battlefield" if is_permanent(type_line) else "graveyard"
        elif kind == "activate":
            player = self.player(e.player)
            assert e.card is not None
            source = self.resolve(e.card, player)
            targets = [self.target(t) for t in e.targets]
            body = {"card_id": f"@{source.label}"}
            if targets:
                body["targets"] = targets
            index = self.activated_index(e)
            if index is not None:
                body["ability_index"] = index
            if player != self.active:
                self.act("pass", self.active)
            self.act("activate", player, body)
            self.passes(player)
        elif kind == "attack":
            defending = self.player(e.player)
            attackers = [self.resolve(a, self.active) for a in e.targets]
            if e.card is not None:
                defender = f"@{self.resolve(e.card, defending).label}"
            else:
                defender = defending
            pairs = [{"creature": f"@{a.label}", "defender": defender} for a in attackers]
            self.passes(self.active)
            self.act("advance", self.active)
            self.steps.append({"decide": {"player": self.active, "answer": {"pairs": pairs}}})
        elif kind == "block":
            assert e.card is not None and e.targets
            blocking = self.opponent(self.active)
            pair = {
                "creature": f"@{self.resolve(e.card, blocking).label}",
                "defender": f"@{self.resolve(e.targets[0], self.active).label}",
            }
            if self._block_step is None:
                self.passes(self.active)
                self._block_step = {"decide": {"player": blocking, "answer": {"pairs": []}}}
                self.steps.append(self._block_step)
            self._block_step["decide"]["answer"]["pairs"].append(pair)
        elif kind == "concede":
            self.act("concede", self.player(e.player))
        else:
            self.steps.append(
                {
                    "note": f"line {e.line_no}: {_text(e)} (not replayed: the engine does this"
                    " itself, or the log hides it)"
                }
            )


def _cast_zone(e: Event) -> str:
    """The zone a cast takes its card from: the graveyard for flashback-style casts."""
    return "graveyard" if "from the graveyard" in e.raw else "hand"


def _text(e: Event) -> str:
    """The event's line(s) as plain text: no markup, timestamps or card brackets."""
    parts = (
        _BRACKETS.sub(r"\1", _TIMESTAMP.sub("", strip_markup(p))).strip()
        for p in e.raw.splitlines()
    )
    return " / ".join(parts) or e.kind


def require_two_players(players: list[str]) -> None:
    """MTGO logs are 1v1 here: the sandbox seats exactly the two players."""
    if len(players) != 2:
        shown = ", ".join(repr(p) for p in players[:6]) + (", ..." if len(players) > 6 else "")
        raise ValueError(
            f"the import needs a log of exactly 2 players, found {len(players)}: {shown}"
        )


def turn_players(events: list[Event]) -> dict[int, str]:
    return {e.turn: e.player for e in events if e.kind == "turn" and e.turn and e.player}


def target_turn(events: list[Event], upto_turn: int | None) -> int:
    """`upto_turn`, or the last turn of the log; ValueError when the log has no such turn."""
    turns = turn_players(events)
    if not turns:
        raise ValueError("the log has no turn header (Turn N: <player>), so there is no turn")
    last = max(turns)
    turn = last if upto_turn is None else upto_turn
    if turn not in turns:
        raise ValueError(
            f"upto_turn {upto_turn} is not in the log: it has turns 1 to {last}"
            " (counted across both players)"
        )
    return turn


def to_scenario(
    players: list[str],
    events: list[Event],
    upto_turn: int | None,
    *,
    type_lines: Mapping[str, str | None] | None = None,
    oracle_texts: Mapping[str, str | None] | None = None,
    lookup_timeout_s: float = 10.0,
) -> tuple[dict[str, Any], list[Unmapped]]:
    """The scenario for the board at the start of `upto_turn` plus that turn's actions.

    `type_lines` maps card names to type lines and `oracle_texts` the names of activated
    cards to oracle text; when omitted they are looked up (bounded by `lookup_timeout_s`).
    Raises ValueError for a turn the log does not have or a log without exactly 2 players.
    """
    turn = target_turn(events, upto_turn)
    require_two_players(players)
    names = safe_player_names(players)
    if type_lines is None:
        type_lines = lookup_type_lines(card_names(players, events), lookup_timeout_s)
    if oracle_texts is None:
        activated = {e.card for e in events if e.kind == "activate" and e.card}
        oracle_texts = lookup_oracle_texts(activated, lookup_timeout_s)
    conv = _Converter(
        names=names, types=type_lines, tokens=token_names(events), oracle=oracle_texts
    )
    conv.active = names[turn_players(events)[turn]]
    unmapped: list[Unmapped] = []

    for e in events:
        if e.turn is None or e.turn < turn:
            try:
                conv.replay(e)
            except _Problem as exc:
                unmapped.append(Unmapped(e.line_no, e.raw, str(exc)))
    start = [(o, o.zone, dict(o.counters)) for o in conv.board.objs]
    renamed = [f"{log!r} is {safe!r}" for log, safe in names.items() if log != safe]
    if renamed:
        conv.steps.append({"note": "player names in the sandbox: " + ", ".join(renamed)})
    for e in events:
        if e.turn != turn:
            continue
        mark = len(conv.steps)
        try:
            conv.emit(e)
        except _Problem as exc:
            del conv.steps[mark:]
            unmapped.append(Unmapped(e.line_no, e.raw, str(exc)))
    start += [(o, zone, {}) for o, zone in conv.setup_adds]

    cards = []
    for obj, zone, counters in start:
        card: dict[str, Any] = {
            "card": obj.name,
            "player": obj.owner,
            "zone": zone,
            "label": obj.label,
        }
        if counters:
            card["counters"] = counters
        if zone == "battlefield":
            card["summoning_sick"] = False  # every permanent here entered on an earlier turn
        cards.append(card)
    scenario = {
        "setup": {
            "players": [{"name": safe, "life": 20} for safe in names.values()],
            "active_player": conv.active,
            "step": "precombat_main",
            "cards": cards,
        },
        "steps": conv.steps,
        "answers": "baseline",
    }
    return scenario, unmapped


def import_log(
    text: str,
    upto_turn: int | None,
    *,
    lookup_timeout_s: float,
) -> tuple[dict[str, Any], set[str]]:
    """The `sim_import_mtgo_log` answer without `run` and support fields, plus the card names
    whose data resolved (the names a support check may read without blocking)."""
    players, events, parse_unmapped = parse_mtgo_log(text)
    turn = target_turn(events, upto_turn)
    require_two_players(players)
    types = lookup_type_lines(card_names(players, events), lookup_timeout_s)
    scenario, convert_unmapped = to_scenario(
        players, events, turn, type_lines=types, lookup_timeout_s=lookup_timeout_s
    )
    unsupported = sorted(parse_unmapped + convert_unmapped, key=lambda u: u.line_no)
    considered = sum(1 for e in events if e.turn is None or e.turn <= turn)
    mapped = considered - len(convert_unmapped)
    used = {c["card"] for c in scenario["setup"]["cards"]}
    answer = {
        "players": players,
        "turns_seen": max(turn_players(events)),
        "scenario": scenario,
        "unsupported": [
            {"line_no": u.line_no, "text": u.text[:MAX_UNSUPPORTED_TEXT], "reason": u.reason}
            for u in unsupported[:MAX_UNSUPPORTED]
        ],
        "summary": f"{mapped} lines mapped, {len(unsupported)} unsupported",
        "board_caveats": BOARD_CAVEATS,
    }
    if len(unsupported) > MAX_UNSUPPORTED:
        answer["unsupported_omitted"] = len(unsupported) - MAX_UNSUPPORTED
    return answer, {n for n in used if types.get(n)}
